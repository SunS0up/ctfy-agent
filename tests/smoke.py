#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测：不联网、不花钱、不用真令牌。

覆盖：MCP 协议（JSON/SSE、401、会话）、语义动作映射、OpenAI 工具转换、
解题循环、flag 幂等与提交上限、合规护栏、状态机迁移。

    python tests/smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from ctfy_agent.testing import fake_llm  # noqa: E402
from ctfy_agent.testing import fake_mcp_server as fake  # noqa: E402
from ctfy_agent.testing.fake_llm import tool_call  # noqa: E402

from ctfy_agent import Config, CtfyPlatform, McpHttpClient, Solver, Store  # noqa: E402
from ctfy_agent.budget import Budget  # noqa: E402
from ctfy_agent.config import load as load_config  # noqa: E402
from ctfy_agent.mcp_client import classify_tool, mask_token, mcp_tool_to_openai  # noqa: E402
from ctfy_agent.solver import _compact_history, _est_tokens, looks_accepted  # noqa: E402
from ctfy_agent.tools_local import LocalTools, ToolError  # noqa: E402
from smoke_ai_chat import case_ai_chat  # noqa: E402
from smoke_http_session import case_http_302_cookie  # noqa: E402
from smoke_context import case_context_compress  # noqa: E402
from smoke_ai_agent import case_ai_agent_e2e  # noqa: E402
from smoke_ai_fixtures import case_ai_fixtures  # noqa: E402
from smoke_ai_role import case_ai_contract, case_ai_wiring  # noqa: E402
from smoke_case_misc import case_misc_ext  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def ok(label: str) -> None:
    PASSED.append(label)
    print(f"PASS {label}")


def make_config(url: str, tmp: Path, **overrides) -> Config:
    config = load_config()
    config.mcp_url = url
    config.token = "pf_test_token_123456"
    config.runs_dir = str(tmp)
    config.model = "deepseek-v4.1-flash"
    config.max_steps = 6
    config.submit_limit = 3
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def seed_challenge(store: Store) -> None:
    store.upsert(
        {
            "id": "web-01",
            "name": "Login Bypass",
            "category": "web",
            "points": 200,
            "description": "登录处存在逻辑缺陷，拿到 admin 后用 flag 提交。",
        }
    )
    store.save()


# ---------- 用例 ----------


def case_protocol(tmp: Path) -> None:
    for mode in ("json", "sse"):
        server, url = fake_llm.start_fake_mcp(mode=mode)
        try:
            client = McpHttpClient(url, "pf_test_token_123456")
            result = client.initialize()
            assert (result or {}).get("serverInfo", {}).get("name") == "fake-ctfy"
            assert client.session_id == fake.SESSION_ID
            tools = client.list_tools()
            assert len(tools) == 6, [t.get("name") for t in tools]
            ok(f"MCP 握手 + tools/list（{mode} 响应）")
        finally:
            server.shutdown()
            server.server_close()


def case_auth(tmp: Path) -> None:
    server, url = fake_llm.start_fake_mcp(expect_token="pf_good")
    try:
        client = McpHttpClient(url, "pf_bad_token")
        try:
            client.initialize()
        except Exception as exc:
            assert getattr(exc, "http_status", None) == 401, exc
            ok("401 令牌错误被识别")
        else:
            raise AssertionError("错误令牌竟然握手成功")
    finally:
        server.shutdown()
        server.server_close()


def case_resolution(tmp: Path) -> None:
    server, url = fake_llm.start_fake_mcp()
    try:
        platform = CtfyPlatform(url, "pf_test_token_123456")
        platform.connect()
        report = platform.resolution_report()
        assert report["list_challenges"] == "list_challenges", report
        assert report["submit_flag"] == "submit_flag", report
        assert report["start_instance"] == "start_instance", report
        assert report["leaderboard"] == "get_leaderboard", report
        ok(f"语义动作自动映射：{report['submit_flag']} / {report['list_challenges']}")

        specs = platform.openai_tools()
        assert all(spec["type"] == "function" for spec in specs)
        submit_spec = next(s for s in specs if s["function"]["name"] == "submit_flag")
        assert submit_spec["function"]["parameters"]["required"] == ["challenge_id", "flag"]
        ok("MCP schema → OpenAI function 规格转换")

        assert classify_tool("search_writeups") == "read"
        assert classify_tool("submit_flag") == "write"
        ok("工具读写分类（写操作不重试）")
    finally:
        server.shutdown()
        server.server_close()


def case_solver(tmp: Path) -> None:
    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "run").load()
        store.challenges.clear()
        seed_challenge(store)

        llm = fake_llm.ScriptedLLM(fake_llm.demo_script())
        events: list[str] = []
        solver = Solver(config, platform, llm, store, on_event=lambda kind, payload=None: events.append(kind))
        summary = solver.solve("web-01")

        assert summary["submitted"] is True, summary
        challenge = store.require("web-01")
        assert challenge.status == "submitted", challenge.status
        assert challenge.attempts == 1, challenge.attempts
        assert challenge.instance, "实例信息没有落到 state"
        assert Path(summary["transcript"]).is_file(), summary["transcript"]
        assert "flag_accepted" in events
        ok("解题主循环跑通：笔记 → 提交 → 状态落盘")

        # 幂等：同一个 flag 再提交一次应当被本地拦截，不产生第二次网络提交
        local_calls_before = challenge.attempts
        text, accepted = solver._submit({"flag": fake.ACCEPT_FLAG}, "web-01")
        assert accepted is False and "已经提交过" in text, text
        assert store.require("web-01").attempts == local_calls_before
        ok("flag 幂等保护：重复提交被拦截且不计次")
    finally:
        server.shutdown()
        server.server_close()


def case_submit_limit(tmp: Path) -> None:
    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp, submit_limit=1)
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "limit").load()
        store.challenges.clear()
        seed_challenge(store)
        solver = Solver(config, platform, fake_llm.ScriptedLLM([]), store)

        solver._submit({"flag": "flag{guess_1}"}, "web-01")
        text, accepted = solver._submit({"flag": "flag{guess_2}"}, "web-01")
        assert accepted is False and "上限" in text, text
        ok("提交上限生效（1 次后续提交被拦截）")
    finally:
        server.shutdown()
        server.server_close()


def case_budget_limits(tmp: Path | None = None) -> None:
    """三层刹车单独验证：token / 调用次数 / 金额。"""
    tokens = Budget(max_tokens=1000, max_calls=3, max_cost=2.0, price_input=1000.0, price_output=1000.0)
    assert tokens.exceeded() is None
    tokens.record({"prompt_tokens": 600, "completion_tokens": 0})
    assert tokens.total_tokens == 600 and tokens.calls == 1
    assert abs(tokens.cost - 0.6) < 1e-9, tokens.cost
    assert tokens.exceeded() is None
    tokens.record({"prompt_tokens": 500, "completion_tokens": 0})
    assert tokens.total_tokens == 1100 and "token" in (tokens.exceeded() or "")

    calls = Budget(max_calls=1)
    calls.record({})
    assert calls.calls == 1 and "调用次数" in (calls.exceeded() or "")

    money = Budget(max_cost=0.5, price_input=1_000_000.0)
    money.record({"prompt_tokens": 600})
    assert abs(money.cost - 600.0) < 1e-9 and "费用" in (money.exceeded() or "")

    # 不配单价就卡不住金额：cost 恒为 0，不会误拦
    free = Budget(max_cost=0.5)
    free.record({"prompt_tokens": 10_000_000})
    assert free.cost == 0.0 and free.exceeded() is None

    # 跨进程续算
    carried = Budget(max_tokens=1000).load({"prompt_tokens": 900, "calls": 7})
    assert carried.prompt_tokens == 900 and carried.calls == 7
    assert carried.exceeded() is None          # 900 < 1000，还剩一点
    carried.record({"prompt_tokens": 200})
    assert "token" in (carried.exceeded() or "")

    # 0 表示不限
    unlimited = Budget(max_tokens=0, max_calls=0, max_cost=0.0)
    unlimited.record({"prompt_tokens": 10**9, "completion_tokens": 10**9})
    assert unlimited.exceeded() is None
    ok("费用刹车：token / 调用次数 / 金额三道上限与跨进程续算")


class BudgetLLM:
    """模拟真实 LLM：每次调用都记账，预算用尽时抛 BudgetExceeded。"""

    def __init__(self, per_call: int = 150_000):
        self.budget = None
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.calls = 0
        self.per_call = per_call

    def chat(self, messages, tools=None, tool_choice="auto"):
        if self.budget is not None:
            self.budget.check()
        self.calls += 1
        usage = {
            "prompt_tokens": self.per_call,
            "completion_tokens": 100,
            "total_tokens": self.per_call + 100,
        }
        for key in self.usage:
            self.usage[key] += usage[key]
        if self.budget is not None:
            self.budget.record(usage)
        return {
            "content": "继续探测",
            "tool_calls": [
                {
                    "id": f"call_{self.calls}",
                    "type": "function",
                    "function": {"name": "note", "arguments": '{"text": "still working"}'},
                }
            ],
            "finish_reason": "tool_calls",
            "usage": usage,
            "raw": {},
        }


def case_budget_gate(tmp: Path) -> None:
    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp, max_tokens_per_challenge=250_000, max_llm_calls=20)
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "budget").load()
        store.challenges.clear()
        seed_challenge(store)

        llm = BudgetLLM(per_call=150_000)   # 每次 15 万 token
        solver = Solver(config, platform, llm, store)
        summary = solver.solve("web-01", max_steps=10)

        assert summary["submitted"] is False, summary
        assert summary["reason"] == "budget_exhausted", summary
        assert llm.calls == 2, llm.calls                     # 第 3 次在发请求前被拦
        assert summary["budget"]["total_tokens"] == 300_200, summary["budget"]
        assert store.require("web-01").tokens["total_tokens"] == 300_200

        # 重跑不能绕过：用量已落盘，第二次开跑前就拒绝，一次请求都不发
        store.save()
        reloaded = Store(tmp / "budget").load()
        platform2 = CtfyPlatform(url, config.token)
        platform2.connect()
        llm2 = BudgetLLM(per_call=150_000)
        summary2 = Solver(config, platform2, llm2, reloaded).solve("web-01")
        assert summary2["reason"] == "budget_exhausted", summary2
        assert llm2.calls == 0, llm2.calls
        assert summary2["steps"] == 0, summary2
        ok("费用刹车：用尽即停，且重跑继承落盘用量、不会重新烧钱")
    finally:
        server.shutdown()
        server.server_close()


def case_crew(tmp: Path) -> None:
    from ctfy_agent.crew import Crew  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        # 给 web 角色指定 "web-script" 模型，好让工厂返回对应的脚本化 LLM
        config.extra["roles"] = {
            "web": {
                "max_steps": 4,
                "allow_shell": False,
                "platform": ["read", "instance", "oracle"],
                "local": ["http_request"],
                "model": "web-script",
            },
        }
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "crew").load()
        store.challenges.clear()
        seed_challenge(store)

        coordinator = fake_llm.ScriptedLLM(
            [
                {
                    "content": "派 web 专家去找 flag",
                    "tool_calls": [tool_call("delegate", {"role": "web", "task": "找出登录页后的 flag"})],
                },
                {
                    "content": "按专家的结论提交",
                    "tool_calls": [tool_call("submit", {"answer": fake.ACCEPT_FLAG})],
                },
                {"content": "收工", "tool_calls": [tool_call("done", {})]},
            ]
        )
        web = fake_llm.ScriptedLLM(
            [
                {"content": "", "tool_calls": [tool_call("get_challenge", {"challenge_id": "web-01"})]},
                {
                    "content": "",
                    "tool_calls": [
                        tool_call(
                            "report",
                            {
                                "text": "题面是 Login Bypass（登录绕过），还没拿到 flag",
                                "evidence": '"name": "Login Bypass"',
                            },
                        )
                    ],
                },
            ]
        )

        def factory(model=None):
            return web if model == "web-script" else coordinator

        events: list[str] = []
        crew = Crew(config, platform, store, factory, on_event=lambda kind, payload=None: events.append(kind))
        summary = crew.solve("web-01")

        assert summary["submitted"] is True, summary
        assert summary["reason"] == "flag_accepted", summary
        assert store.require("web-01").status == "submitted"
        notes = "\n".join(store.require("web-01").notes)
        assert "Login Bypass" in notes, notes            # 专家结论进了共享笔记
        assert "[evidence|" in notes, notes              # M4：结论带可溯源出处
        assert "delegate" in events and "report" in events
        # 逐调用 usage 落盘：协调者与专家每次 chat 的用量都随转录记一行（观测缺口补上）
        recs = [
            json.loads(ln)
            for ln in store.transcript_path("web-01").read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        with_usage = [r for r in recs if "usage" in r]
        assert with_usage, "转录里没有任何带 usage 的记录（逐调用 usage 落盘失效）"
        assert all("prompt_tokens" in (r.get("usage") or {}) for r in with_usage), with_usage[0]
        ok("多智能体协作：协调者派活 → 专家交结论（带逐字证据）→ 协调者提交")
    finally:
        server.shutdown()
        server.server_close()


def case_role_extension(tmp: Path) -> None:
    from ctfy_agent.crew import Crew, _apply_llm_spec  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        # 一个"新题型"角色：只写配置，不改代码
        config.extra["roles"] = {
            "pwn": {
                "model": "some-model",
                "max_steps": 6,
                "allow_shell": True,
                "platform": ["read", "get_leaderboard"],  # 功能组名 + 直写工具名
                "local": ["shell"],
                "system": "你是 pwn 专家。",
                "max_tokens": 8192,
                "temperature": 0.0,
            },
        }
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "ext").load()
        crew = Crew(config, platform, store, lambda model=None: fake_llm.ScriptedLLM([]))

        # 1) 平台工具：功能组名与直写工具名都认，未授权的仍然不给
        names = crew._role_platform_names("pwn")
        assert "get_challenge" in names, names        # 来自 read 组
        assert "get_leaderboard" in names, names      # 直写工具名
        assert "submit_flag" not in names, names      # 没授权的照旧拒绝

        # 2) delegate 的候选角色从 roles 派生，新角色立刻可派活
        tools = crew._coordinator_tools()
        delegate = next(t for t in tools if t["function"]["name"] == "delegate")
        enum = delegate["function"]["parameters"]["properties"]["role"]["enum"]
        assert "pwn" in enum, enum
        assert "coordinator" not in enum, enum        # 不能自己派给自己
        assert "web" in enum, enum                    # 内置角色仍在

        # 3) 自定义提示词生效，且自动补上交付规则；已写过的不重复追加
        prompt = Crew._role_system("pwn", "你是 pwn 专家。")
        assert prompt.startswith("你是 pwn 专家。"), prompt
        assert "report" in prompt, prompt
        assert Crew._role_system("pwn", "结论用 report 交回。").count("report") == 1

        # 4) 角色级采样参数能盖到客户端上
        llm = fake_llm.ScriptedLLM([])
        _apply_llm_spec(llm, {"max_tokens": 8192, "temperature": 0.0})
        assert llm.max_tokens == 8192 and llm.temperature == 0.0
        ok("题型扩展接口：组名/直写工具名、delegate 枚举派生、自定义提示词、角色级采样参数")
    finally:
        server.shutdown()
        server.server_close()


def case_role_extension_guard(tmp: Path) -> None:
    """新角色也不能变相拿到提交权。"""
    from ctfy_agent.crew import Crew  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        config.extra["roles"] = {
            "evil": {"platform": ["submit", "submit_flag", "submit_answer"], "local": []},
        }
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "ext2").load()
        crew = Crew(config, platform, store, lambda model=None: fake_llm.ScriptedLLM([]))
        names = crew._role_platform_names("evil")
        # 假服务器只有 submit_flag；真服务器才有 submit_answer。两者都该按白名单裁掉。
        assert "submit_answer" not in names, names
        ok("新角色授权仍受平台工具白名单约束")
    finally:
        server.shutdown()
        server.server_close()


def case_auto_batch(tmp: Path) -> None:
    from ctfy_agent.auto import run_auto  # noqa: PLC0415

    store = Store(tmp / "auto").load()
    store.challenges.clear()
    for cid in ("a-1", "a-2", "a-3", "a-4"):
        store.upsert({"id": cid, "name": cid})
    # 直接设状态：走 upsert 会受状态机约束（parsed→submitted 本就该被拒）
    for cid, status in (("a-1", "parsed"), ("a-2", "parsed"), ("a-3", "stalled"), ("a-4", "submitted")):
        store.require(cid).status = status

    # 1) 只挑未解状态，已提交的不重跑
    picked = [c["challenge_id"] for c in run_auto(store=store, solve=lambda cid: {"submitted": True})["results"]]
    assert picked == ["a-1", "a-2", "a-3"], picked

    # 2) 单题抛异常不中断整批
    calls = []

    def flaky(cid):
        calls.append(cid)
        if cid == "a-2":
            raise RuntimeError("boom")
        return {"submitted": True, "budget": {"total_tokens": 10}}

    summary = run_auto(store=store, solve=flaky, max_failures=5)
    assert summary["attempted"] == 3 and summary["failed"] == 1, summary
    assert calls == ["a-1", "a-2", "a-3"], calls

    # 3) 连续失败到阈值即停
    summary = run_auto(store=store, solve=lambda cid: (_ for _ in ()).throw(RuntimeError("x")), max_failures=2)
    assert summary["stopped"] == "too_many_failures", summary
    assert summary["attempted"] == 2, summary

    # 4) 全局 token 上限生效
    summary = run_auto(store=store, solve=lambda cid: {"submitted": True, "budget": {"total_tokens": 60}}, max_total_tokens=100)
    assert summary["stopped"] == "total_budget", summary
    assert summary["spent"] == 120, summary   # 第 2 题跑完就超了，第 3 题不再起

    # 5) limit 生效
    assert len(run_auto(store=store, solve=lambda cid: {"submitted": True}, limit=1)["results"]) == 1

    # 6) tokens_spent 优先于累计 budget：重试返回累计值也只按本轮 delta 计（审计 B9）
    summary = run_auto(store=store, solve=lambda cid: {"submitted": True, "budget": {"total_tokens": 9999}, "tokens_spent": 7}, max_total_tokens=1000)
    assert summary["spent"] == 21, summary["spent"]   # 3 题 × 7，而非 3 × 9999
    ok("无人值守批量：状态筛选、失败隔离、连续失败熔断、全局额度、数量上限")
    return


def case_instance_release(tmp: Path) -> None:
    """未解出时自动释放实例：团队配额只有 1 个，不释放下一题就起不来。"""
    from ctfy_agent.crew import Crew  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        config.extra["roles"] = {"coordinator": {"max_steps": 2}, "web": {"max_steps": 2}}
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "release").load()
        store.challenges.clear()
        seed_challenge(store)

        started: list[str] = []
        stopped: list[str] = []
        platform.start_instance = lambda cid, comp=None: (
            started.append(cid),
            {"id": "inst-9", "host": "10.0.0.9", "port": 31002},
        )[1]
        platform.stop_instance = lambda iid: (stopped.append(iid), {"state": "stopped"})[1]

        crew = Crew(config, platform, store, lambda model=None: fake_llm.ScriptedLLM([{"content": "算了"}]))
        summary = crew.solve("web-01")

        assert summary["submitted"] is False, summary
        assert started == ["web-01"], started
        assert stopped == ["inst-9"], stopped                    # 失败后确实释放了
        assert store.require("web-01").instance_id == "", store.require("web-01").instance_id
        ok("实例释放：未解出时自动 stop，避免占满团队唯一实例配额")
    finally:
        server.shutdown()
        server.server_close()


def case_shell_python(tmp: Path) -> None:
    """shell 里的 python 必须指向真实解释器（宿主 PATH 上是商店占位符）。"""
    import json  # noqa: PLC0415

    store = Store(tmp / "shellpy").load()
    store.challenges.clear()
    seed_challenge(store)
    local = LocalTools(store, "web-01", allow_shell=True)
    out = json.loads(local.call("shell", {"command": 'python -c "print(6*7)"'}))
    assert out["exit_code"] == 0, out
    assert "42" in out["stdout"], out
    ok("shell 内 python 可用（PATH 已指向真实解释器）")
    return


def case_guard(tmp: Path) -> None:
    config = load_config()
    store = Store(tmp / "guard").load()
    store.challenges.clear()
    seed_challenge(store)
    local = LocalTools(store, "web-01")

    try:
        local.http_request("http://example.com/")
    except ToolError as exc:
        assert "还没有登记" in str(exc) or "合规拦截" in str(exc), exc
        ok("未登记实例时 http_request 被拒")
    else:
        raise AssertionError("没有实例也放行了")

    local.allow_instance("10.0.0.9", 31001)
    try:
        local.http_request("http://evil.example.com/")
    except ToolError as exc:
        assert "合规拦截" in str(exc), exc
        ok("非下发实例的域名被合规护栏拦截")
    else:
        raise AssertionError("第三方站点被放行")


def case_hardening(tmp: Path) -> None:
    # M1：自然语言里的否定不再被误判为通过
    assert looks_accepted("Your flag was not accepted, try again") is False
    assert looks_accepted("you are correct, great job") is None  # 不再从自由文本推断「通过」
    ok("提交判定：not accepted 判拒绝，自由文本不再误判通过")

    store = Store(tmp / "hard").load()
    store.challenges.clear()
    seed_challenge(store)
    local = LocalTools(store, "web-01")

    # M3：登记端口后，同主机其他端口仍被拦（端口不再是装饰）
    local.allow_instance("10.0.0.9", 31001)
    try:
        local.http_request("http://10.0.0.9:9999/")
    except ToolError as exc:
        assert "合规拦截" in str(exc), exc
        ok("端点级白名单：登记 31001 后访问 9999 被拦")
    else:
        raise AssertionError("同主机其他端口被放行")

    # M3b：https 服务登记 host:443 后，不写端口的 https://host/ 应按协议默认端口放行
    local.allow_instance("tls.example.com", 443)
    local._check_target("https://tls.example.com/")   # 不抛即放行
    try:
        local._check_target("http://tls.example.com/")  # http 默认 80，未登记，应拦
        raise AssertionError("http 协议打在 443 主机上被放行")
    except ToolError:
        pass
    ok("https 443：不写端口按协议默认端口对齐，异协议仍被拦")

    # M3c：http_request 的 grep 只回匹配片段，不整段回（大响应搜 flag 用，防上下文爆炸）
    out = LocalTools._render_http(200, {}, "a" * 500 + "flag{abc}" + "b" * 500, r"flag\{[^}]+\}")
    assert "flag{abc}" in out and '"matched": 1' in out
    assert "a" * 200 not in out, "grep 模式把整段正文带回来了"
    ok("http grep：只回匹配片段，不整段回")

    # M3d：grep 命中过多时截断，不全量物化（审计 B11）
    # 上限从 150 降到 30：30×~175 字符 ≈ 5.2KB，整张表能塞进 6000 的可见上限
    out = LocalTools._render_http(200, {}, ("x" * 20 + "\n") * 300, "x+")
    assert '"matched": 30' in out and '"truncated": true' in out, out

    # M3e：正则过长直接拒（审计 B12，stdlib re 无超时的尽力缓解）
    try:
        LocalTools._render_http(200, {}, "abc", "a" * 300)
        raise AssertionError("超长正则被放行")
    except ToolError as exc:
        assert "过长" in str(exc), exc

    # M3f：越界/非法端口要报 ToolError，不能崩（审计 B14）
    try:
        local._check_target("http://10.0.0.9:99999/")
        raise AssertionError("越界端口被放行")
    except ToolError as exc:
        assert "端口非法" in str(exc), exc

    # H1：重定向到未登记主机被护栏拦截（不再盲目跟随）
    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102 - 静默访问日志
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://evil.example.com/steal")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        local.allow_instance("127.0.0.1", port)
        try:
            local.http_request(f"http://127.0.0.1:{port}/")
        except ToolError as exc:
            assert "合规拦截" in str(exc), exc
            ok("重定向护栏：302 到第三方站点被拦截")
        else:
            raise AssertionError("重定向绕过护栏")
    finally:
        server.shutdown()
        server.server_close()

    # L1：run_id 路径穿越被拒绝
    config = make_config("http://127.0.0.1:1/v1/mcp/", tmp)
    try:
        config.run_dir("../evil")
    except ValueError:
        ok("run_id 路径穿越被拒绝")
    else:
        raise AssertionError("run_id 穿越被放行")
    assert config.run_dir("ok_1").name == "ok_1"

    # L3：掩码只露前缀，不露尾部
    masked = mask_token("pf_secret_token_12345678")
    assert "12345678" not in masked and masked.startswith("pf_s"), masked
    ok("凭据掩码不再暴露尾部字符")


def case_compact(tmp: Path) -> None:
    head = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    msgs = list(head)
    for i in range(6):
        msgs.append({"role": "assistant", "content": f"step{i}", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "http_request", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "X" * 6000})

    full = _est_tokens(msgs)
    assert full > 9000, full
    trimmed = _compact_history(msgs, 9000)
    assert trimmed[0] == head[0] and trimmed[1] == head[1]
    assert _est_tokens(trimmed) < full
    # M5 成本优化后：超软阈值先压缩旧步（条数不变、token 变少），超硬阈值才整块丢；
    # 两种都算「压缩生效」，这里断言副作用确实发生。
    assert len(trimmed) < len(msgs) or _est_tokens(trimmed) < _est_tokens(msgs)

    seen = {c["id"] for m in trimmed if m["role"] == "assistant" for c in (m.get("tool_calls") or [])}
    for m in trimmed:
        if m["role"] == "tool":
            assert m["tool_call_id"] in seen, "tool 消息被孤立（配对被拆散）"

    assert _compact_history(msgs[:4], 1_000_000) == msgs[:4]
    ok("上下文滑窗：超窗丢最旧整块、保留 head、不拆散 tool 配对")


def case_state_machine(tmp: Path) -> None:
    store = Store(tmp / "sm").load()
    store.challenges.clear()
    seed_challenge(store)
    store.set_status("web-01", "parsed")
    try:
        store.set_status("web-01", "submitted")
    except ValueError as exc:
        assert "不允许" in str(exc)
        ok("状态机拒绝非法跳转（parsed → submitted）")
    else:
        raise AssertionError("非法状态迁移被放行")

    assert looks_accepted('{"accepted": true}') is True
    assert looks_accepted("wrong flag") is False
    assert looks_accepted("hmm") is None
    # 真实平台 verify_answer 返回裸布尔（实测 ctfy 3.4.6）
    assert looks_accepted("----- 工具返回 -----\ntrue\n{\n  \"result\": true\n}") is True
    assert looks_accepted("----- 工具返回 -----\nfalse\n{\n  \"result\": false\n}") is False
    ok("提交结果判定（accepted/拒绝/未知三态 + 裸布尔）")


def case_role_deep_merge(tmp: Path) -> None:
    """部分覆盖内置角色时，未写的键沿用默认值（浅合并会把整条默认吞掉，审计 B2）。"""
    from ctfy_agent.crew import Crew  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        config.extra["roles"] = {"web": {"model": "web-script"}}   # 只给 model，其余键不写
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "merge").load()
        crew = Crew(config, platform, store, lambda model=None: fake_llm.ScriptedLLM([]))

        merged = crew.roles["web"]
        assert merged["model"] == "web-script", merged
        assert merged.get("platform"), merged          # 默认平台组仍在
        assert merged.get("local"), merged            # 默认本地工具仍在
        assert merged.get("max_steps") and merged.get("allow_shell") is False, merged
        ok("角色深合并：部分覆盖内置角色时默认键仍保留（审计 B2）")
    finally:
        server.shutdown()
        server.server_close()


def case_submit_name(tmp: Path) -> None:
    """系统提示里的提交工具名必须跟随平台实际解析结果，而非写死（审计 B13）。"""
    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        config.extra["auto_start_instance"] = False
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        orig = platform.try_resolve

        def fake_resolve(action, default=None):
            # 模拟真实平台：提交工具叫 submit_answer，不是 submit_flag
            return "submit_answer" if action == "submit_flag" else orig(action, default)

        platform.try_resolve = fake_resolve
        store = Store(tmp / "submitname").load()
        store.challenges.clear()
        seed_challenge(store)
        llm = fake_llm.ScriptedLLM([])
        Solver(config, platform, llm, store).solve("web-01")

        system = llm.received[0][0]["content"]
        assert "submit_answer" in system, system
        assert "submit_flag" not in system, system
        ok("提交工具名动态化：系统提示用平台实际工具名（审计 B13）")
    finally:
        server.shutdown()
        server.server_close()


def case_release_on_submit(tmp: Path) -> None:
    """提交成功后也要释放实例：配额=1，不释放下一题起不来（审计 B20）。"""
    from ctfy_agent.crew import Crew  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "rel_submit").load()
        store.challenges.clear()
        seed_challenge(store)

        stopped: list[str] = []
        platform.start_instance = lambda cid, comp=None: {"id": "inst-7", "host": "10.0.0.7", "port": 31002}
        platform.stop_instance = lambda iid: (stopped.append(iid), {"state": "stopped"})[1]

        script = [
            {"content": "直接提交", "tool_calls": [tool_call("submit", {"answer": fake.ACCEPT_FLAG})]},
            {"content": "收工", "tool_calls": [tool_call("done", {})]},
        ]
        crew = Crew(config, platform, store, lambda model=None: fake_llm.ScriptedLLM(script))
        summary = crew.solve("web-01")

        assert summary["submitted"] is True, summary
        assert stopped == ["inst-7"], stopped          # 成功后也释放了
        assert store.require("web-01").instance_id == ""
        ok("实例释放：提交成功后也释放（审计 B20）")
    finally:
        server.shutdown()
        server.server_close()


def case_http_offset_cache(tmp: Path) -> None:
    """Fix 1 + 6：offset/Range 本地化 + 重复读桩 + 同 host 缓存。"""
    import json as _json

    full = "".join(chr(0x21 + (i % 90)) for i in range(15000))  # 15000 字符、无换行的可打印串
    state = {"get_hits": 0, "range_header": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102
            pass

        def do_GET(self):  # noqa: N802
            state["get_hits"] += 1
            state["range_header"] = self.headers.get("Range")
            body = full.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)  # 先消费请求体，再回包，否则连接会被重置
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        store = Store(tmp / "httpcache").load()
        store.challenges.clear()
        seed_challenge(store)
        local = LocalTools(store, "web-01", cache={})
        local.allow_instance("127.0.0.1", port)
        url = f"http://127.0.0.1:{port}/app.js"

        # 1) Range 头被本地转成 offset：服务器收不到 Range（不回 416），返回正确窗口
        out = _json.loads(local.http_request(url, headers={"Range": "bytes=5000-9999"}))
        assert out["status"] == 200 and state["range_header"] is None, (out, state["range_header"])
        assert out["offset"] == 5000 and out["body"] == full[5000:10000], (out["offset"], out["body"][:12])
        assert out["truncated"] is True and out["next_offset"] == 10000, out

        # 2) offset 参数从缓存切片读尾段，不再发网络
        hits = state["get_hits"]
        out2 = _json.loads(local.http_request(url, offset=10000))
        assert out2["body"] == full[10000:15000], out2["body"][:12]
        assert out2.get("truncated") is None, out2
        assert state["get_hits"] == hits, (state["get_hits"], hits)

        # 3) 完全重复读（无 offset/grep）→ 缓存桩，不再回全文、不发网络
        stub = _json.loads(local.http_request(url))
        assert stub["cached"] is True and stub["total_bytes"] == 15000, stub
        assert state["get_hits"] == hits

        # 4) 显式 offset 仍从缓存切片
        out3 = _json.loads(local.http_request(url, offset=7000))
        assert out3["body"] == full[7000:12000], out3["body"][:12]
        assert state["get_hits"] == hits

        # 5) 非 GET 清同 host 缓存：下一次 GET 重新拉取
        local.http_request(url, method="POST", body="x=1")
        hits = state["get_hits"]
        out4 = _json.loads(local.http_request(url))
        assert state["get_hits"] == hits + 1, (state["get_hits"], hits)
        assert out4.get("cached") is None, out4

        # 6) offset 越界 → past_end，不崩
        out5 = _json.loads(local.http_request(url, offset=99999))
        assert out5["body"] == "" and out5.get("past_end") is True, out5
        ok("http offset/Range 本地化 + body 缓存/重复读桩/同 host 清除")
    finally:
        server.shutdown()
        server.server_close()


def case_http_cookie_jar(tmp: Path) -> None:
    """新增：http_request 会话 cookie jar —— 登录 Set-Cookie 自动回传，显式 Cookie 优先，缓存按会话区分。"""
    import json as _json

    state = {"cookie_seen": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102
            pass

        def _send(self, code, body, headers=()):
            data = body.encode("utf-8")
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if self.path == "/logout":
                self._send(200, '{"ok":true}', (("Set-Cookie", "session="),))
            else:
                self._send(200, '{"ok":true}', (
                    ("Set-Cookie", "session=abc123; Path=/; HttpOnly; Expires=Wed, 21 Oct 2026 07:28:00 GMT"),
                    ("Set-Cookie", "tag=a,b,c; Path=/"),
                ))

        def do_GET(self):  # noqa: N802
            ck = self.headers.get("Cookie")
            state["cookie_seen"] = ck
            self._send(200, '{"user":"admin"}' if ck and "session=abc123" in ck else '{"user":"anon"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        store = Store(tmp / "httpcookie").load()
        store.challenges.clear()
        seed_challenge(store)
        local = LocalTools(store, "web-01", cache={})
        local.allow_instance("127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"

        # 登录前：匿名
        out0 = _json.loads(local.http_request(base + "/whoami"))
        assert out0["body"] == '{"user":"anon"}', out0
        assert state["cookie_seen"] is None, state["cookie_seen"]

        # 登录：POST 回两条 Set-Cookie（一条 Expires 带逗号日期，一条值里含逗号，审计 B4）
        local.http_request(base + "/login", method="POST", body="u=1")
        jar = local._jar.get(f"127.0.0.1:{port}", {})
        assert jar == {"session": "abc123", "tag": "a,b,c"}, jar

        # 登录后：GET 自动带 Cookie
        out1 = _json.loads(local.http_request(base + "/whoami"))
        assert out1["body"] == '{"user":"admin"}', out1
        assert "session=abc123" in (state["cookie_seen"] or ""), state["cookie_seen"]
        assert "tag=a,b,c" in (state["cookie_seen"] or ""), state["cookie_seen"]

        # 显式 Cookie 头优先于 jar（模型换会话不被 jar 锁死）
        out2 = _json.loads(local.http_request(base + "/whoami", headers={"Cookie": "session=other"}))
        assert state["cookie_seen"] == "session=other", state["cookie_seen"]

        # 登出：空值 Set-Cookie 从 jar 删除（审计 B5），而非存成空串继续回传
        local.http_request(base + "/logout", method="POST", body="")
        assert "session" not in local._jar.get(f"127.0.0.1:{port}", {}), local._jar

        ok("http_request cookie jar：Set-Cookie 自动回传(含 Expires 逗号/值内逗号) + 显式 Cookie 优先 + 空值删除 + 缓存按会话区分")
    finally:
        server.shutdown()
        server.server_close()


def case_http_diff(tmp: Path) -> None:
    """新增：http_request diff —— 同 host+同 path（忽略 query）基线做行级差异，服务 IDOR 枚举。"""
    import json as _json

    state = {"hits": 0}

    def users_body(uid: int) -> str:
        """多行 JSON：改 id 只该有 id/name/email 三行变化，role 是稳定行（可当上下文）。"""
        return "\n".join(
            [
                "{",
                f'  "id": {uid},',
                f'  "name": "user{uid}",',
                f'  "email": "u{uid}@example.test",',
                '  "role": "user"',
                "}",
            ]
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102
            pass

        def _send(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            state["hits"] += 1
            path, _, qs = self.path.partition("?")
            query = dict(item.split("=", 1) for item in qs.split("&") if "=" in item)
            if path == "/api/user":
                self._send(users_body(int(query.get("id") or 1)))
            elif path == "/big":
                variant = query.get("v") or "1"
                self._send("\n".join(f'{{"k": "{variant}-{i:04d}"}}' for i in range(400)))
            else:
                self._send('{"fresh": true}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        store = Store(tmp / "httpdiff").load()
        store.challenges.clear()
        seed_challenge(store)
        local = LocalTools(store, "web-01", cache={})
        local.allow_instance("127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"

        # 0) schema 里真的挂着 diff（模型才看得见这个开关）
        http_spec = next(s for s in local.specs() if s["function"]["name"] == "http_request")
        props = http_spec["function"]["parameters"]["properties"]
        assert props["diff"]["type"] == "boolean", props.get("diff")

        # 1) 首次请求（无基线）：普通窗口输出，不打 baseline_had 假标记
        first = _json.loads(local.http_request(base + "/api/user?id=1"))
        assert "baseline_had" not in first and '"name": "user1"' in first["body"], first
        hits = state["hits"]

        # 2) 改 id + diff=true（字符串 "true"，走 call 分发）：只回变化行
        out = _json.loads(local.call("http_request", {"url": base + "/api/user?id=2", "diff": "true"}))
        assert state["hits"] == hits + 1, (state["hits"], hits)   # diff 绕过缓存读，必然联网
        assert out["status"] == 200 and out["baseline_had"] is True, out
        assert out["added_lines"] == 3 and out["removed_lines"] == 3, (out["added_lines"], out["removed_lines"])
        assert '-  "name": "user1",' in out["diff"] and '+  "name": "user2",' in out["diff"], out["diff"]
        assert "body" not in out, out          # 不回全文，这才是省 token 的点
        assert out["truncated"] is False, out

        # 3) 同 URL 再 diff：仍要重新联网，且自己和自己比 → 无差异
        hits = state["hits"]
        same = _json.loads(local.http_request(base + "/api/user?id=2", diff=True))
        assert state["hits"] == hits + 1, (state["hits"], hits)
        assert same["baseline_had"] is True and same["diff"] == "", same
        assert same["added_lines"] == 0 and same["removed_lines"] == 0, same
        assert "没有变化" in same.get("note", ""), same

        # 4) diff 结果写回缓存：随后普通读命中缓存桩（基线链条在推进）
        stub = _json.loads(local.http_request(base + "/api/user?id=2"))
        assert stub["cached"] is True, stub

        # 5) 新 path 没基线 → 退回普通窗口 + baseline_had=false
        fresh = _json.loads(local.http_request(base + "/fresh", diff=True))
        assert fresh["baseline_had"] is False and '"fresh": true' in fresh["body"], fresh
        assert fresh["added_lines"] == 0 and fresh["removed_lines"] == 0, fresh

        # 6) 差异超长 → 截断到约 4000 字符，但计数仍是全量
        local.http_request(base + "/big?v=1")
        big = _json.loads(local.http_request(base + "/big?v=2", diff=True))
        assert big["truncated"] is True, big
        assert big["added_lines"] == 400 and big["removed_lines"] == 400, (big["added_lines"], big["removed_lines"])
        assert 1000 < len(big["diff"]) <= 4000, len(big["diff"])

        ok("http_request diff：同 path 基线行级差异 + 绕过缓存读 + 无基线兜底 + 超长截断")
    finally:
        server.shutdown()
        server.server_close()


def case_run_role_synthesis(tmp: Path) -> None:
    """Fix 2：专家步数耗尽时合成一份带动作清单的报告，别只交一行占位符。"""
    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "synth").load()
        store.challenges.clear()
        seed_challenge(store)
        solver = Solver(config, platform, fake_llm.ScriptedLLM([]), store)

        # 一直调 note、从不交 report；最后一步还留了半成品思考
        llm = fake_llm.ScriptedLLM(
            [
                {"content": "", "tool_calls": [tool_call("note", {"text": "a"})]},
                {"content": "", "tool_calls": [tool_call("note", {"text": "b"})]},
                {"content": "我怀疑 flag 在 /admin 的 JS 包里", "tool_calls": [tool_call("note", {"text": "c"})]},
            ]
        )
        result = solver.run_role(
            "web-01",
            role="web",
            llm=llm,
            system="s",
            task="t",
            specs=[],
            max_steps=3,
            allow_shell=False,
            platform_names=solver._platform_names(),
        )
        assert result["reason"] == "step_limit", result
        assert "最近动作" in result["report"] and "note" in result["report"], result
        assert "最后思考" in result["report"] and "/admin" in result["report"], result
        ok("run_role 步数耗尽合成报告：动作清单 + 最后思考交还协调者")
    finally:
        server.shutdown()
        server.server_close()


def case_rescue_report(tmp: Path) -> None:
    """P1-1 救援收口：步数耗尽后最后一发请求交回可执行结论（reason=rescued）。"""
    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "rescue").load()
        store.challenges.clear()
        seed_challenge(store)
        solver = Solver(config, platform, fake_llm.ScriptedLLM([]), store)

        # 3 步一直 note、不交 report；第 4 条脚本是救援请求的收口结论
        llm = fake_llm.ScriptedLLM(
            [
                {"content": "", "tool_calls": [tool_call("note", {"text": "a"})]},
                {"content": "", "tool_calls": [tool_call("note", {"text": "b"})]},
                {"content": "", "tool_calls": [tool_call("note", {"text": "c"})]},
                {"content": "/admin 返回 401，疑似需 JWT；下一步爆破弱口令。", "tool_calls": []},
            ]
        )
        result = solver.run_role(
            "web-01",
            role="web",
            llm=llm,
            system="s",
            task="t",
            specs=[],
            max_steps=3,
            allow_shell=False,
            platform_names=solver._platform_names(),
        )
        assert result["reason"] == "rescued", result
        assert "JWT" in result["report"], result
        assert llm.calls == 4, llm.calls  # 3 步 + 1 次救援
        ok("救援收口：步数耗尽后最后一发请求交回结论（reason=rescued）")
    finally:
        server.shutdown()
        server.server_close()


def case_ensure_instance(tmp: Path) -> None:
    """Fix 3：实例幂等启动（先查再启）、429 退避、模型侧重复 start 拦截。"""
    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)

        # --- 子用例 1：平台已有本题实例 → 复用，不再 start ---
        pa = CtfyPlatform(url, config.token)
        pa.connect()
        sa = Store(tmp / "ens_a").load()
        sa.challenges.clear()
        seed_challenge(sa)
        started_a: list[str] = []
        pa.try_resolve = lambda kind: {
            "start_instance": "start_instance",
            "list_instances": "list_instances",
            "submit_flag": "submit_flag",
            "stop_instance": "stop_instance",
        }.get(kind)
        pa.call_text = lambda name, args=None, **kw: (
            '[{"id": "inst-existing", "challenge_id": "web-01", "host": "10.0.0.9", "port": 31001}]'
            if name == "list_instances"
            else "{}"
        )

        def must_not_start(cid, comp=None):
            started_a.append(cid)
            raise AssertionError("已有实例，不应再 start")

        pa.start_instance = must_not_start
        solva = Solver(config, pa, fake_llm.ScriptedLLM([]), sa)
        info = solva._ensure_instance("web-01")
        assert info and info.get("id") == "inst-existing", info
        assert started_a == [] and sa.require("web-01").instance_id == "inst-existing", started_a

        # 模型侧又调 start → 幂等拦截，仍不打到平台
        local = LocalTools(sa, "web-01")
        local.allow_many(info)
        text, accepted = solva._execute("start_instance", {}, local, "web-01")
        assert accepted is False and "已在运行" in text, text
        assert started_a == []

        # --- 子用例 2：限流两次后第三次成功（指数退避）---
        pb = CtfyPlatform(url, config.token)
        pb.connect()
        sb = Store(tmp / "ens_b").load()
        sb.challenges.clear()
        seed_challenge(sb)
        start_calls: list[str] = []

        def flaky_start(cid, comp=None):
            start_calls.append(cid)
            if len(start_calls) < 3:
                return "[工具报错] Rate limited by upstream API, please retry later"
            return {"id": "inst-3", "host": "10.0.0.9", "port": 31001}

        pb.start_instance = flaky_start
        solvb = Solver(config, pb, fake_llm.ScriptedLLM([]), sb)
        info2 = solvb._ensure_instance("web-01")
        assert info2 and info2.get("id") == "inst-3", info2
        assert len(start_calls) == 3 and sb.require("web-01").instance_id == "inst-3", start_calls
        ok("实例幂等启动（先查再启）+ 429 退避 + 模型侧重复 start 拦截")
    finally:
        server.shutdown()
        server.server_close()


def case_windows_shell_guard(tmp: Path) -> None:
    """Fix 4b：Windows 上 Unix-only 内建命令/heredoc/单引号被明确拦截并指回正路。"""
    if os.name != "nt":
        return  # 容器是 Linux，这些命令都在；只有本地 Windows 开发需要此护栏
    store = Store(tmp / "winshell").load()
    store.challenges.clear()
    seed_challenge(store)
    local = LocalTools(store, "web-01", allow_shell=True)

    try:
        local.call("shell", {"command": "curl -sL 'http://x' | tail -c 8000"})
        raise AssertionError("Unix-only 命令在 Windows 上没被拦")
    except ToolError as exc:
        assert "tail" in str(exc) and "http_request" in str(exc), exc
        assert "单引号" in str(exc), exc  # 单引号提示也在
        ok("Windows shell 护栏：Unix 内建命令 + 单引号被明确拦截并指回 http_request")


def case_prompt_hints(tmp: Path) -> None:
    """Fix 5：SPA grep 套路与通用大响应 offset 指引都进了提示词。"""
    from ctfy_agent.crew import Crew  # noqa: PLC0415
    from ctfy_agent.solver import SYSTEM_PROMPT  # noqa: PLC0415

    web = Crew._role_system("web")
    assert "单页应用" in web and "grep" in web, web
    assert "大响应" in SYSTEM_PROMPT and "offset" in SYSTEM_PROMPT, SYSTEM_PROMPT
    ok("提示词：SPA grep 套路 + 大响应 offset 指引已内置")


def case_limit_and_kwargs(tmp: Path) -> None:
    """新增：http_request 支持 limit（clamp 200..5000）+ 未知参数过滤 + 缺参友好报错。"""
    import json as _json

    store = Store(tmp / "limitkw").load()
    store.challenges.clear()
    seed_challenge(store)
    local = LocalTools(store, "web-01", cache={})

    # 1) limit 生效：窗口按请求截断，翻页锚点随之变化
    payload = "x" * 10000
    out = _json.loads(LocalTools._render_http(200, {}, payload, "", 0, None, limit=300))
    assert out["body"] == "x" * 300 and out["truncated"] is True and out["next_offset"] == 300, out
    # 2) limit 夹到 [200, 5000]
    assert len(_json.loads(LocalTools._render_http(200, {}, payload, "", 0, None, limit=99999))["body"]) == 5000
    assert len(_json.loads(LocalTools._render_http(200, {}, payload, "", 0, None, limit=1))["body"]) == 200

    # 3) 未知参数被过滤 + 回显合法清单，不再 TypeError 崩步
    res = local.call("note", {"text": "abc", "bogus_param": 7})
    assert "已忽略未知参数" in res and "bogus_param" in res and "合法参数" in res, res
    assert "已记录" in res, res

    # 4) 缺必需参数 → 友好 ToolError（带清单），不是裸 [执行异常]
    try:
        local.call("note", {})
        raise AssertionError("应当抛 ToolError")
    except ToolError as exc:
        assert "参数错误" in str(exc) and "合法参数" in str(exc), exc
    ok("limit 窗口 + 未知参数过滤 + 缺参友好报错")


def case_prior_digest(tmp: Path) -> None:
    """新增：跨轮结论摘要（堵住『跨代重复侦察/重复凭据』的结构性缺口）。"""
    from ctfy_agent.crew import _prior_digest  # noqa: PLC0415

    assert _prior_digest([]) == ""
    assert "abc" in _prior_digest(["abc"]), _prior_digest(["abc"])
    # 只带最近 3 轮
    digest = _prior_digest(["r1", "r2", "r3", "r4"])
    assert "r4" in digest and "r1" not in digest, digest
    # 单轮截断到 cap，且带「已失败尝试勿重复」锚点
    digest = _prior_digest(["tried admin/admin"] + ["y" * 5000], cap=600)
    assert "已失败尝试" in digest and "admin/admin" in digest, digest
    assert len(digest) < 1500, len(digest)
    ok("跨轮结论摘要 _prior_digest")


def case_multi_question(tmp: Path) -> None:
    """多问题/门控题：brief 列出全部题号并要求逐题提交；取题失败静默退化。"""
    from ctfy_agent.crew import Crew  # noqa: PLC0415
    from ctfy_agent.state import Store  # noqa: PLC0415

    store = Store(tmp / "multiq").load()
    store.challenges.clear()
    seed_challenge(store)
    item = store.require("web-01")

    crew = Crew.__new__(Crew)  # 只测文本拼装，不建实例、不联网
    crew.roles = {"coordinator": {}, "web": {}}
    crew._question_info = {
        "flag_solves": {"slug": 23, "flag": 24},
        "questions": [{"id": "slug", "prompt": "the 32-hex slug"}],
    }
    brief = crew._brief(item)
    assert "多问题题" in brief and "slug" in brief and "flag" in brief, brief
    assert "challenge_fully_solved=false" in brief, brief
    assert "不要调用 done" in brief, brief

    # 单问题题不能加噪音
    crew._question_info = {"flag_solves": {"flag": 17}}
    assert "多问题题" not in crew._brief(item), crew._brief(item)

    # 平台没有 get_challenge 时静默返回 {}，不击穿主流程
    class _NoTool:
        @staticmethod
        def try_resolve(_name):
            return None

    crew.platform = _NoTool()
    assert crew._fetch_challenge("web-01", object()) == {}

    assert Crew._question_ids({"flag_solves": {"a": 1, "b": 2}}) == ["a", "b"]
    ok("多问题题：brief 列出全部题号并提示逐题提交；单问题不加噪音；取题失败静默退化")


def case_stdlib_only(tmp: Path) -> None:  # noqa: ARG001 - 签名与其它用例统一
    """容器契约：镜像不装任何依赖且删了 pip，src 里出现第三方 import 就是 ImportError → 0 分。"""
    import ast  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415

    src = Path(__file__).resolve().parents[1] / "src"
    pkg_dir = src / "ctfy_agent"
    stdlib = set(_sys.stdlib_module_names)
    internal: set[str] = {"ctfy_agent"}
    files = sorted(pkg_dir.rglob("*.py"))
    assert files, "没扫到源码"
    for f in files:
        rel = f.relative_to(src).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        internal.add(".".join(parts))

    bad: list[str] = []
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in stdlib or top in internal:
                    continue
                bad.append(f"{f.name}:{node.lineno} -> {name}")
    assert not bad, "容器里会 ImportError 的第三方 import：\n  " + "\n  ".join(bad)
    ok(f"容器契约：源码纯 stdlib（{len(files)} 个文件，无第三方 import）")


def case_challenge_meta(tmp: Path) -> None:
    """A0/M1：题型线索（tags/difficulty/solves_count）入库，并确定性送达三处提示。"""
    from ctfy_agent.cli import ensure_challenge_meta, normalize_challenge  # noqa: PLC0415
    from ctfy_agent.crew import Crew  # noqa: PLC0415
    from ctfy_agent.solver import Solver  # noqa: PLC0415
    from ctfy_agent.state import Challenge, normalize_tags  # noqa: PLC0415

    # 1) tags 宽容归一（平台可能给 list / 逗号串 / 混合），去重保序
    assert normalize_tags(["weak_password", "osint"]) == ["weak_password", "osint"]
    assert normalize_tags("weak_password, osint") == ["weak_password", "osint"]
    assert normalize_tags(["weak_password", "weak_password", "osint"]) == ["weak_password", "osint"]
    assert normalize_tags(None) == [] and normalize_tags("") == []

    # 2) normalize_challenge 三字段都映射；tags 不再冒充 category（审计坑 2）
    normalized = normalize_challenge(
        {
            "id": "CTFARENA-035",
            "name": "StartupDeck",
            "category": "web",
            "points": 100,
            "difficulty": "easy",
            "solves_count": 10,
            "tags": ["owasp_2025_a07", "weak_password", "osint", "social_engineering"],
        }
    )
    assert normalized["difficulty"] == "easy" and normalized["solves_count"] == 10, normalized
    assert "weak_password" in normalized["tags"] and "osint" in normalized["tags"], normalized
    only_tags = normalize_challenge({"id": "x", "name": "y", "tags": ["weak_password"]})
    assert only_tags["category"] is None and only_tags["tags"] == ["weak_password"], only_tags

    # 3) upsert 合并（tags 并集，不是覆盖）；老 state.json 向后兼容
    store = Store(tmp / "metastate").load()
    store.challenges.clear()
    store.upsert({"id": "web-01", "name": "Login Bypass", "tags": ["osint"]})
    item = store.upsert(
        {"id": "web-01", "difficulty": "easy", "solves_count": 10, "tags": ["weak_password", "osint"]}
    )
    assert item.tags == ["osint", "weak_password"], item.tags
    assert item.difficulty == "easy" and item.solves_count == 10, item
    legacy = Challenge.from_dict({"id": "old", "tags": "weak_password"})
    assert legacy.tags == ["weak_password"] and legacy.difficulty == "" and legacy.solves_count is None, legacy

    # 4) facets 单行文本（brief 与专家背景共用）
    facets = item.facets()
    assert "难度 easy" in facets and "已解 10 队" in facets and "weak_password" in facets, facets

    # 5) M1：协调者 brief / 专家背景 / 单 agent 初始提示三处都带 weak_password
    crew = Crew.__new__(Crew)  # 只测文本拼装，不建实例、不联网
    crew.roles = {"coordinator": {}, "web": {}}
    brief = crew._brief(item)
    assert "weak_password" in brief and "难度 easy" in brief and "已解 10 队" in brief, brief
    context = Solver._role_context(item)  # crew 专家拿不到 Challenge，靠这个数据块
    assert "weak_password" in context and "难度 easy" in context, context
    initial = Solver._initial_prompt(item, LocalTools(store, "web-01"))
    assert "weak_password" in initial and "难度 easy" in initial, initial

    # 6) 老 state（题目已在库、但没题型线索）会补一次 get_challenge；有线索则不再打平台
    server, url = fake_llm.start_fake_mcp()
    try:
        platform = CtfyPlatform(url, "pf_test_token_123456")
        platform.connect()
        stale = Store(tmp / "metabackfill").load()
        stale.challenges.clear()
        seed_challenge(stale)  # 老 state.json 的形态：只有 name/category/points/description
        assert stale.require("web-01").tags == [] and stale.require("web-01").difficulty == ""
        assert ensure_challenge_meta(platform, stale, "web-01") is None
        filled = stale.require("web-01")
        assert "weak_password" in filled.tags and filled.difficulty == "easy", filled
        assert filled.solves_count == 10, filled.solves_count

        class _NoCall:
            def get_challenge(self, challenge_id):  # noqa: ARG002
                raise AssertionError("已有题型线索时不该再调 get_challenge")

        assert ensure_challenge_meta(_NoCall(), stale, "web-01") is None
        assert "取题目失败" in ensure_challenge_meta(_NoCall(), stale, "not-a-challenge")
    finally:
        server.shutdown()
        server.server_close()
    ok("A0/M1：题型线索入库 → brief/专家背景/初始提示三处送达 + 老 state 自动补齐")


def case_hint_scope(tmp: Path) -> None:
    """A0 附带项：提示类工具 list_hints / view_hint 真正进了模型工具面。"""
    from ctfy_agent.crew import PLATFORM_SCOPE, Crew  # noqa: PLC0415
    from ctfy_agent.platform import MODEL_TOOLS  # noqa: PLC0415

    hints = {"list_hints", "view_hint"}
    # 单 agent 路径靠 MODEL_TOOLS（_platform_names 只认它）；crew 路径靠 PLATFORM_SCOPE
    assert hints <= MODEL_TOOLS, sorted(MODEL_TOOLS)
    assert hints <= PLATFORM_SCOPE["read"], sorted(PLATFORM_SCOPE["read"])
    for bad in ("submit_registration", "register_for_competition", "contact_organisers",
                "upload_authored_challenge", "join_awd_arena"):
        assert bad not in MODEL_TOOLS, bad

    # crew 按角色过滤：read 组能拿到 hint，危险工具仍进不来
    class _Stub:
        @staticmethod
        def tool_names():
            return ["get_challenge", "list_hints", "view_hint", "submit_registration"]

    crew = Crew.__new__(Crew)
    crew.roles = {"web": {"platform": ["read"]}}
    crew.platform = _Stub()
    names = crew._role_platform_names("web")
    assert hints <= names and "submit_registration" not in names, names

    # list_hints 需要 challenge_id + competition_id：_autofill 会自动补齐，模型不用自己拼
    store = Store(tmp / "hintmeta").load()
    store.challenges.clear()
    seed_challenge(store)
    store.meta["competition_id"] = "comp-test-1"

    class _HintPlatform:
        @staticmethod
        def prop_names(name):
            return {"challenge_id", "competition_id"} if name == "list_hints" else set()

    solver = Solver(make_config("http://127.0.0.1:1", tmp), _HintPlatform(), fake_llm.ScriptedLLM([]), store)
    filled = solver._autofill("list_hints", {}, "web-01")
    assert filled == {"challenge_id": "web-01", "competition_id": "comp-test-1"}, filled
    ok("提示工具已放行（list_hints/view_hint）+ 参数自动补齐，危险工具仍拦截")


def case_ledger(tmp: Path) -> None:
    """A1/M2+M3：去重键含归一化 args、假设轮换 open→refuted、拦截与渲染。"""
    from ctfy_agent.ledger import (  # noqa: PLC0415
        Ledger,
        attempt_key,
        attempt_label,
        normalize_url,
    )

    # 1) 归一化：同一次尝试的不同写法必须同键（大小写/默认端口/query 顺序/JSON 键序/空白）
    assert normalize_url("HTTPS://Example.COM:443/api/login?b=2&a=1") == "https://example.com/api/login?a=1&b=2"
    assert normalize_url("http://h:80/x") == "http://h/x"
    assert normalize_url("h/x") == "http://h/x"
    body_loose = {"method": "post", "url": "https://h/api/login", "body": '{"password":"admin","username":"admin"}'}
    body_strict = {"method": "POST", "url": "https://H/api/login", "body": {"username": "admin", "password": "admin"}}
    assert attempt_key("http_request", body_loose) == attempt_key("http_request", body_strict)
    other = dict(body_strict, body={"username": "admin", "password": "toast"})
    assert attempt_key("http_request", other) != attempt_key("http_request", body_strict)
    # 不同投影（offset）算不同尝试，不算重复
    assert attempt_key("http_request", dict(body_strict, offset=0)) != attempt_key(
        "http_request", dict(body_strict, offset=5000)
    )
    # 凭据出现在人读标签里（上一轮「只记结果不记 args」的根因就靠这个堵）
    assert "admin" in attempt_label("http_request", body_strict), attempt_label("http_request", body_strict)

    # 2) 记录 / 去重 / 渲染
    ledger = Ledger(refute_after=2)
    assert ledger.seen("http_request", body_strict) is None
    ledger.record("http_request", body_strict, '401 {"error":"invalid credentials"}', role="web")
    hit = ledger.seen("http_request", body_loose)
    assert hit is not None and hit.role == "web" and hit.repeats == 1
    assert ledger.note_duplicate(hit.key) == 1 and hit.repeats == 2
    digest = ledger.digest()
    assert "已试过" in digest and "admin" in digest and "401" in digest, digest
    assert ledger.stats()["duplicates_intercepted"] == 1, ledger.stats()

    # 3) 方向证伪：同簇 2 个不同候选全失败 → refuted，且注入一次「禁止再试」
    assert ledger.refuted() == []
    ledger.record("http_request", other, '401 {"error":"invalid credentials"}', role="web")
    refuted = ledger.refuted()
    assert len(refuted) == 1 and refuted[0].cluster == "http POST /api/login", refuted
    assert refuted[0].failures == 2 and refuted[0].refuted_at is not None
    assert len(ledger.drain_new_refutations()) == 1
    assert ledger.drain_new_refutations() == []  # 只注入一次
    rotate = ledger.rotate()
    assert "禁止再试" in rotate and "2 个不同候选" in rotate and "POST /api/login" in rotate, rotate

    # 成功过的方向不会被证伪（避免把对的方向误杀）
    ok_ledger = Ledger(refute_after=2)
    ok_ledger.record("http_request", body_strict, "200 OK", role="web")
    ok_ledger.record("http_request", other, '401 {"error":"invalid credentials"}', role="web")
    assert ok_ledger.refuted() == [], ok_ledger.hypotheses

    # 4) 证据自动收录（flag 形态，逐字）
    assert any("flag{" in obs.text for obs in ledger.observations) is False
    ledger.record("http_request", {"url": "https://h/api/login", "body": "{}"}, "welcome FLAG{ex4mple_value} done")
    assert any("FLAG{ex4mple_value}" in obs.text for obs in ledger.observations), ledger.observations

    # 5) Solver 侧接线：拦截文本 / report 不拦 / 证伪后返回轮换文本
    class _P:
        @staticmethod
        def try_resolve(kind):
            return "submit_answer" if kind == "submit_flag" else None

    store = Store(tmp / "ledgerstate").load()
    store.challenges.clear()
    seed_challenge(store)
    solver = Solver(make_config("http://127.0.0.1:1", tmp), _P(), fake_llm.ScriptedLLM([]), store)

    gate_ledger = Ledger(refute_after=99)
    args = {"url": "https://h/api/login", "method": "POST", "body": {"username": "a", "password": "b"}}
    assert solver._ledger_gate(gate_ledger, "http_request", args) is None
    solver._ledger_after(gate_ledger, "http_request", args, '401 invalid credentials', role="web")
    blocked = solver._ledger_gate(gate_ledger, "http_request", dict(args))
    assert blocked and "ledger 拦截" in blocked and "未再执行" in blocked, blocked
    assert solver._ledger_gate(gate_ledger, "report", args) is None  # 交结论永不被拦
    assert solver._ledger_gate(gate_ledger, "submit_answer", args) is None  # 提交永不被拦

    refute_ledger = Ledger(refute_after=2)
    first = solver._ledger_after(refute_ledger, "http_request", dict(args, body="1"), "401 invalid", role="web")
    second = solver._ledger_after(refute_ledger, "http_request", dict(args, body="2"), "401 invalid", role="web")
    assert first == "" and "假设轮换" in second and "禁止再试" in second, (first, second)
    ok("A1/M2/M3：args 去重键 + 重复拦截 + 方向证伪轮换 + 证据逐字收录")


def case_ledger_crew(tmp: Path) -> None:
    """A1/M2：跨 delegate 同一调用只执行一次，且第二个专家拿到账本。"""
    import json as _json  # noqa: PLC0415

    from ctfy_agent.crew import Crew  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        config.extra["roles"] = {
            "web": {"max_steps": 4, "allow_shell": False, "platform": ["read"], "local": [], "model": "web-script"},
        }
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "ledgercrew").load()
        store.challenges.clear()
        seed_challenge(store)
        store.transcript_path("web-01").unlink(missing_ok=True)  # 转录追加写，清掉上次残留

        coordinator = fake_llm.ScriptedLLM(
            [
                {"content": "派 web 查题面", "tool_calls": [tool_call("delegate", {"role": "web", "task": "查一下题面"})]},
                {"content": "再派一次同样的活", "tool_calls": [tool_call("delegate", {"role": "web", "task": "查一下题面"})]},
                {"content": "收工", "tool_calls": [tool_call("done", {})]},
            ]
        )
        # 两个 delegate 共用同一个脚本化 LLM：每个都先 get_challenge 再 report
        web = fake_llm.ScriptedLLM(
            [
                {"content": "", "tool_calls": [tool_call("get_challenge", {"challenge_id": "web-01"})]},
                {"content": "", "tool_calls": [tool_call("report", {"text": "题面已读"})]},
                {"content": "", "tool_calls": [tool_call("get_challenge", {"challenge_id": "web-01"})]},
                {"content": "", "tool_calls": [tool_call("report", {"text": "题面已读"})]},
            ]
        )

        def factory(model=None):
            return web if model == "web-script" else coordinator

        crew = Crew(config, platform, store, factory)
        summary = crew.solve("web-01")

        stats = summary["ledger"]
        # 第二个 delegate 的 get_challenge 被账本拦下，没有打到平台
        assert stats["duplicates_intercepted"] >= 2, stats  # 1 次重复派活 + 1 次重复工具调用
        assert stats["attempts"] >= 2, stats

        records = [
            _json.loads(line)
            for line in store.transcript_path("web-01").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        intercepted = [
            rec for rec in records
            if rec.get("tool") == "get_challenge" and "ledger 拦截" in str(rec.get("result") or "")
        ]
        assert intercepted, "第二个 delegate 的重复 get_challenge 没有被拦截"
        assert any(rec.get("role") == "ledger" and "stats" in rec for rec in records), "账本快照没落转录"

        # 第二个专家的初始 user 消息里带账本（含第一个专家试过的参数）
        second_delegate_msgs = web.received[2]
        task_text = second_delegate_msgs[1]["content"]
        assert "确定性账本" in task_text and "get_challenge" in task_text, task_text[:400]
        ok("A1/M2：跨 delegate 重复调用被拦截 + 账本随任务注入")
    finally:
        server.shutdown()
        server.server_close()


def case_report_evidence(tmp: Path) -> None:
    """A2/M4：声称有发现必须有逐字证据；无证据/改写证据被拒，拒绝有上限不死循环。"""
    from ctfy_agent.ledger import Ledger  # noqa: PLC0415
    from ctfy_agent.tools_local import NOTE_SPEC, REPORT_SPEC  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "evidence").load()
        store.challenges.clear()
        seed_challenge(store)
        spinner = store.transcript_path("web-01")
        spinner.unlink(missing_ok=True)  # 转录是追加写的：清掉跨次运行的残留，否则计数翻倍
        solver = Solver(config, platform, fake_llm.ScriptedLLM([]), store)

        # 1) 单元层：三种判定
        ledger = Ledger()
        ledger.record("get_challenge", {"challenge_id": "web-01"},
                      json.dumps({"id": "web-01", "name": "Login Bypass"}), role="web")
        reject_no_ev = solver._check_report(ledger, "发现了 admin 的弱口令", "", 0)
        assert reject_no_ev[0] and "声称" in reject_no_ev[0], reject_no_ev
        reject_fake = solver._check_report(ledger, "发现了 admin 的弱口令", "password is golden", 0)
        assert reject_fake[0] and "逐字" in reject_fake[0] and "Login Bypass" in reject_fake[0], reject_fake
        accepted, source = solver._check_report(ledger, "题面确认登录绕过", '"name": "Login Bypass"', 0)
        assert accepted is None and source.startswith("get_challenge"), (accepted, source)
        assert any("Login Bypass" in obs.text for obs in ledger.observations), ledger.observations
        # 否定型结论不需要证据（失败题属正常）
        negative = solver._check_report(ledger, "该方向 18 次均 401，未拿到凭据", "", 0)
        assert negative == (None, ""), negative
        # 拒绝次数用尽 → 放行（避免为一份 report 烧光步数）
        assert solver._check_report(ledger, "发现了 admin 的弱口令", "", 2) == (None, "")

        # 2) 端到端：无证据被拒 → 改写证据被拒 → 逐字证据通过
        specs = platform.openai_tools_for({"get_challenge"}) + [NOTE_SPEC, REPORT_SPEC]
        llm = fake_llm.ScriptedLLM(
            [
                {"content": "", "tool_calls": [tool_call("get_challenge", {"challenge_id": "web-01"})]},
                {"content": "", "tool_calls": [tool_call("report", {"text": "找到了 flag：" + fake.ACCEPT_FLAG})]},
                {
                    "content": "",
                    "tool_calls": [
                        tool_call(
                            "report",
                            {"text": "找到了 flag：" + fake.ACCEPT_FLAG, "evidence": "password is golden"},
                        )
                    ],
                },
                {
                    "content": "",
                    "tool_calls": [
                        tool_call(
                            "report",
                            {"text": "题面确认 Login Bypass，疑似登录绕过", "evidence": '"name": "Login Bypass"'},
                        )
                    ],
                },
            ]
        )
        run_ledger = Ledger()
        result = solver.run_role(
            "web-01", role="web", llm=llm, system="你是 web 专家", task="看看题面",
            specs=specs, max_steps=6, platform_names={"get_challenge"}, ledger=run_ledger,
        )
        assert result["reason"] == "reported", result
        assert "Login Bypass" in result["report"] and "[evidence|" in result["report"], result["report"]
        assert any("Login Bypass" in obs.text for obs in run_ledger.observations), run_ledger.observations

        records = [
            json.loads(line)
            for line in store.transcript_path("web-01").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rejected = [rec for rec in records if rec.get("tool") == "report_rejected"]
        assert len(rejected) == 2, f"应拒绝 2 次（无证据 + 改写证据），实际 {len(rejected)}"
        ok("A2/M4：report 逐字证据契约（无证据/改写被拒，逐字通过并溯源）")
    finally:
        server.shutdown()
        server.server_close()


def case_plan_companion(tmp: Path) -> None:
    """A2/M6：PLAN/REFLECTION 解析 + 「计划不变且无新尝试 = 停滞」的确定性判定。"""
    import json as _json  # noqa: PLC0415

    from ctfy_agent.companion import PlanTrack, parse_companion  # noqa: PLC0415

    # 1) 解析要宽容：Markdown 强调、全角冒号、列表符号、多行值
    plain = parse_companion("先看页面。\nPLAN: 读打包 JS 找 admin 逻辑\nREFLECTION: 首页只是 SPA 兜底")
    assert plain["PLAN"] == "读打包 JS 找 admin 逻辑", plain
    assert plain["REFLECTION"] == "首页只是 SPA 兜底", plain
    styled = parse_companion("**PLAN：**继续猜凭据\n- REFLECTION: 18 次全 401")
    assert styled["PLAN"] == "继续猜凭据" and styled["REFLECTION"] == "18 次全 401", styled
    multiline = parse_companion("PLAN: 先读首页\n再读 JS\nREFLECTION: ok")
    assert multiline["PLAN"] == "先读首页 再读 JS", multiline
    assert parse_companion("随便说点什么") == {"PLAN": "", "REFLECTION": ""}

    # 2) 状态机：变化的计划算演进，不变的计划 + 无新尝试算停滞
    track = PlanTrack(stall_after=3, max_reminders=2)
    first = track.observe("PLAN: 读 JS\nREFLECTION: 首页是兜底", progressed=True)
    assert first["changed"] is True and first["stall_rounds"] == 0, first
    for _ in range(2):
        seen = track.observe("PLAN: 读 JS\nREFLECTION: 还是 401", progressed=False)
        assert seen["changed"] is False
    assert track.stalled() is False, "未到阈值不该提示"
    track.observe("PLAN: 读 JS\nREFLECTION: 还是 401", progressed=False)
    assert track.stalled() is True
    hint = track.stall_hint()
    assert "[停滞]" in hint and "3 步" in hint and "读 JS" in hint, hint
    assert track.stall_hint() == "", "一个停滞 episode 只提示一次"
    track.observe("PLAN: 读 JS\nREFLECTION: 换了参数", progressed=True)
    assert track.stall_rounds == 0 and track.stalled() is False, "有新尝试应解除停滞"
    track.observe("PLAN: 改用 /api/team 的名字拼凭据\nREFLECTION: 换方向", progressed=True)
    assert track.plan_changes == 2, track.stats()

    # 3) 缺标签提醒有配额（不因凑格式烧步数）
    loose = PlanTrack(max_reminders=2)
    assert loose.observe("我继续看看", progressed=True)["missing"] is True
    assert "[格式]" in loose.reminder() and "[格式]" in loose.reminder()
    assert loose.reminder() == "", "提醒配额用尽后必须闭嘴"
    assert loose.stats()["missing_steps"] == 1

    # 4) 端到端：单 agent 循环里真的注入了提醒与停滞提示
    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "plan").load()
        store.challenges.clear()
        seed_challenge(store)
        transcript = store.transcript_path("web-01")
        transcript.unlink(missing_ok=True)  # 转录是追加写的：清掉跨次运行的残留

        same_plan = "PLAN: 反复读同一份题面\nREFLECTION: 没有新信息"
        script = [
            # 第 1 步：没写 PLAN（只调工具）→ 应触发格式提醒
            {"content": "先看看题目。", "tool_calls": [tool_call("get_challenge", {"challenge_id": "web-01"})]},
            # 第 2-5 步：同一 PLAN + 同一调用（第 2 步起被账本拦下，= 无新尝试）→ 第 5 步判停滞
            {"content": same_plan, "tool_calls": [tool_call("get_challenge", {"challenge_id": "web-01"})]},
            {"content": same_plan, "tool_calls": [tool_call("get_challenge", {"challenge_id": "web-01"})]},
            {"content": same_plan, "tool_calls": [tool_call("get_challenge", {"challenge_id": "web-01"})]},
            {"content": same_plan, "tool_calls": [tool_call("get_challenge", {"challenge_id": "web-01"})]},
            {"content": "收工", "tool_calls": []},
        ]
        llm = fake_llm.ScriptedLLM(script)
        solver = Solver(config, platform, llm, store)
        summary = solver.solve("web-01")

        records = [
            _json.loads(line)
            for line in transcript.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        hints = [rec["content"] for rec in records if rec.get("role") == "plan"]
        assert any("[格式]" in text for text in hints), hints
        assert any("[停滞]" in text for text in hints), hints
        # PLAN 落成独立字段，M6 不用再解析文本
        planned = [rec for rec in records if rec.get("role") == "assistant" and rec.get("plan")]
        assert planned and planned[0]["plan"] == "反复读同一份题面", planned[:1]
        assert summary["plan"]["stall_events"] == 1, summary["plan"]
        assert summary["plan"]["missing_steps"] == 1, summary["plan"]
        ok("A2/M6：PLAN 解析 + 停滞判定 + 格式提醒/停滞提示注入（有界）")
    finally:
        server.shutdown()
        server.server_close()


def case_delegate_objective(tmp: Path) -> None:
    """A2：协调者派活改成「一个 objective + done_when」，旧 task 字段仍兼容。"""
    import json as _json  # noqa: PLC0415

    from ctfy_agent.crew import Crew, _delegate_task  # noqa: PLC0415

    # 1) 任务头拼装
    task = _delegate_task("用 /api/team 的名字试 /api/login", "拿到 200 响应体")
    assert "【本轮目标】用 /api/team 的名字试 /api/login" in task, task
    assert "【完成判据】拿到 200 响应体" in task, task
    assert "report" in task, task
    no_done = _delegate_task("先读首页", "")
    assert "未给出" in no_done and "report" in no_done, no_done

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        config.extra["roles"] = {
            "coordinator": {"max_steps": 12},  # 本用例要跑到停滞阈值之后
            "web": {"max_steps": 3, "allow_shell": False, "platform": ["read"], "local": [], "model": "web-script"},
        }
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "delegateobj").load()
        store.challenges.clear()
        seed_challenge(store)
        transcript = store.transcript_path("web-01")
        transcript.unlink(missing_ok=True)

        same_plan = "PLAN: 让 web 确认登录接口\nREFLECTION: 题面提到 /admin"
        coord = {"role": "web"}
        obj = "确认 /api/login 的实际响应"

        def dup(done: str) -> dict:
            return {"content": same_plan,
                    "tool_calls": [tool_call("delegate", {**coord, "objective": obj, "done_when": done})]}

        coordinator = fake_llm.ScriptedLLM(
            [
                # 正常：一个 objective + done_when
                {
                    "content": same_plan,
                    "tool_calls": [
                        tool_call(
                            "delegate",
                            {
                                "role": "web",
                                "objective": obj,
                                "done_when": "拿到 /api/login 的响应体或明确的状态码",
                            },
                        )
                    ],
                },
                # 同 objective、只改 done_when 措辞 → 仍算重复派活
                dup("换个写法的同一个判据"),
                # 旧格式（只有 task）→ 兼容
                {"content": same_plan, "tool_calls": [tool_call("delegate", {"role": "web", "task": "旧格式任务"})]},
                # 连续重复派活 → 触发协调者侧停滞提示
                dup("第三个判据"),
                dup("第四个判据"),
                dup("第五个判据"),
                # 什么都没有 → 拒绝，且不派活
                {"content": same_plan, "tool_calls": [tool_call("delegate", {"role": "web"})]},
                {"content": "收工", "tool_calls": [tool_call("done", {})]},
            ]
        )
        web = fake_llm.ScriptedLLM(
            [
                {"content": "", "tool_calls": [tool_call("report", {"text": "未发现新信息，仅确认接口存在"})]},
                {"content": "", "tool_calls": [tool_call("report", {"text": "未发现新信息，仍是同一判据"})]},
                {"content": "", "tool_calls": [tool_call("report", {"text": "旧格式也能派活，未发现新信息"})]},
                {"content": "", "tool_calls": [tool_call("report", {"text": "未发现新信息，第三个判据"})]},
                {"content": "", "tool_calls": [tool_call("report", {"text": "未发现新信息，第四个判据"})]},
                {"content": "", "tool_calls": [tool_call("report", {"text": "未发现新信息，第五个判据"})]},
            ]
        )

        def factory(model=None):
            return web if model == "web-script" else coordinator

        crew = Crew(config, platform, store, factory)
        summary = crew.solve("web-01")

        # 专家真的收到了 objective + done_when（结构化任务头）
        first_task = web.received[0][1]["content"]
        assert "【本轮目标】确认 /api/login 的实际响应" in first_task, first_task[:400]
        assert "【完成判据】拿到 /api/login 的响应体或明确的状态码" in first_task, first_task[:400]
        # 只改 done_when 不算新工作：第二次派活带 ledger 警告
        second_task = web.received[1][1]["content"]
        assert "[ledger]" in second_task and "同一 objective" in second_task, second_task[-300:]
        # 旧格式兼容
        legacy_task = web.received[2][1]["content"]
        assert "【本轮目标】旧格式任务" in legacy_task, legacy_task[:300]

        records = [
            _json.loads(line)
            for line in transcript.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rejected = [rec for rec in records if "缺少 objective" in str(rec.get("result") or "")]
        assert rejected, "缺 objective 的 delegate 应被拒绝"
        assert summary["ledger"]["duplicates_intercepted"] >= 4, summary["ledger"]
        # 协调者侧也受「计划不变 + 无新派活目标 = 停滞」约束
        coord_hints = [rec["content"] for rec in records if rec.get("role") == "plan"]
        assert any("[停滞]" in text for text in coord_hints), coord_hints
        assert summary["plan"]["stall_events"] >= 1, summary["plan"]
        # 协调者的 PLAN 也落进转录（M6）
        assert any(rec.get("role") == "coordinator" and rec.get("plan") for rec in records)
        ok("A2：delegate 派活＝一个 objective + done_when（旧 task 兼容、缺 objective 拒绝、改判据仍算重复）")
    finally:
        server.shutdown()
        server.server_close()


def case_audit_regression(tmp: Path) -> None:
    """审计回归（2026-10-25）：不同 objective 不判重、非法端口降级不击穿 run、note/report 不进账本。

    这三条是对审计报告里 #1/#2/#3 的反向断言——原 46 个用例只测了机制的正向行为，
    没测「不同目标不判重」「归一化异常不击穿循环」，缺陷恰好都藏在这两个盲区里。
    """
    from ctfy_agent.ledger import Ledger, attempt_key, normalize_url  # noqa: PLC0415

    # 1) delegate 去重键必须含 objective（审计 #2）：不同 objective 不得判重
    led = Ledger()
    led.record("delegate", {"role": "web", "objective": "读题面"}, "r1", role="coordinator", ok=True)
    assert led.seen("delegate", {"role": "web", "objective": "完全不同的新目标"}) is None
    led.record("delegate", {"role": "web", "objective": "另一个新目标"}, "r2", role="coordinator", ok=True)
    assert len(led.order) == 2, len(led.order)  # 两次不同派活 = 两条，而非折叠成 1 条
    assert "objective" in attempt_key("delegate", {"role": "web", "objective": "读题面"})
    assert led.seen("delegate", {"role": "web", "objective": "读题面"}) is not None  # 同 objective 仍判重

    # 2) 非法端口归一化降级，不再抛 ValueError（审计 #1 的根）
    assert normalize_url("http://host:99999/x") == "http://host/x"
    assert normalize_url("http://host:abc/x") == "http://host/x"

    # 3) note/report 不进账本（审计 #3）：记忆/结论不是尝试，不能伪造「推进」信号
    class _P:
        @staticmethod
        def try_resolve(kind):
            return "submit_answer" if kind == "submit_flag" else None

    store = Store(tmp / "auditreg").load()
    store.challenges.clear()
    seed_challenge(store)
    solver = Solver(make_config("http://127.0.0.1:1", tmp), _P(), fake_llm.ScriptedLLM([]), store)
    nled = Ledger()
    assert solver._ledger_after(nled, "note", {"text": "线索A"}, "已记录") == ""
    assert solver._ledger_after(nled, "report", {"text": "结论", "evidence": ""}, "ok") == ""
    assert len(nled.order) == 0, len(nled.order)
    solver._ledger_after(nled, "http_request", {"url": "https://h/x"}, "200 OK", role="web")
    assert len(nled.order) == 1, len(nled.order)  # 真实工具仍照常记账

    # 4) 端到端：坏 URL 不再击穿 solve（审计 #1 复现，修复后应正常返回而非崩溃）
    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        config.max_steps = 4
        config.extra["auto_start_instance"] = False
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        s2 = Store(tmp / "auditreg_e2e").load()
        s2.challenges.clear()
        seed_challenge(s2)
        llm = fake_llm.ScriptedLLM(
            [
                {"content": "先访问目标", "tool_calls": [
                    tool_call("http_request", {"url": "http://127.0.0.1:99999/", "method": "GET"})
                ]},
                {"content": "收工", "tool_calls": []},
            ]
        )
        solver2 = Solver(config, platform, llm, s2)
        summary = solver2.solve("web-01")  # 不抛异常即通过
        assert summary.get("reason") is not None, summary
    finally:
        server.shutdown()
        server.server_close()
    ok("审计回归：不同 objective 不判重 + 非法端口降级不击穿 + note/report 不进账本")


PROJECT = HERE.parent
BUILTIN_TOOL_NAMES = {"http_request", "note", "shell", "report"}


def _load_outsourced_role() -> dict | None:
    """读接单方交回的 roles/misc.json（兼容 {"roles": {...}} 与直接给 role spec）。"""
    role_file = PROJECT / "roles" / "misc.json"
    prompt_file = PROJECT / "roles" / "misc.system.txt"
    if not role_file.is_file():
        return None
    data = json.loads(role_file.read_text(encoding="utf-8"))
    roles = data.get("roles") if isinstance(data, dict) and "roles" in data else data
    spec = dict((roles or {}).get("misc") or {})
    if not spec.get("system") and prompt_file.is_file():
        spec["system"] = prompt_file.read_text(encoding="utf-8")
    return spec or None


def _outsourced_tools() -> list[tuple[str, dict]]:
    """找出 tools_local 里「不是内置的」SPEC —— 那就是接单方新增的工具。"""
    from ctfy_agent import tools_local  # noqa: PLC0415

    found: list[tuple[str, dict]] = []
    for attr in dir(tools_local):
        if not attr.endswith("_SPEC"):
            continue
        spec = getattr(tools_local, attr)
        if not isinstance(spec, dict):
            continue
        name = ((spec.get("function") or {}).get("name")) or ""
        if name and name not in BUILTIN_TOOL_NAMES:
            found.append((name, spec))
    return found


def case_misc_contract(tmp: Path) -> None:
    """外包模块接入契约：**有产物就强校验，没产物就跳过**（保持基线绿）。

    这是「交回来的 misc 模块能不能接进框架」的判定器（工单 §5.2/§9 引用它）：
      1. `roles/misc.json` 能被 Crew 吃下：角色可见、delegate enum 有它、工具面派生正确、
         提示词自动补齐 report/evidence 规则；
      2. 新增本地工具符合 SPEC 契约：同名方法、参数名一致、返回 ≤6000 字符、只抛 ToolError；
      3. `tests/fixtures/misc/*/solve.py` 能确定性复现 `expected.txt` 的 flag（且没作弊直接读答案）；
      4. 新工具同样受账本去重约束（同参二次调用被拦）。
    """
    import inspect  # noqa: PLC0415
    import re  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    from ctfy_agent.crew import Crew  # noqa: PLC0415
    from ctfy_agent.ledger import Ledger  # noqa: PLC0415
    from ctfy_agent.tools_local import LocalTools, ToolError  # noqa: PLC0415

    role_spec = _load_outsourced_role()
    tools = _outsourced_tools()
    fixtures_dir = PROJECT / "tests" / "fixtures" / "misc"
    fixtures = sorted(p for p in fixtures_dir.glob("*/solve.py")) if fixtures_dir.is_dir() else []

    if not role_spec and not tools and not fixtures:
        ok("外包接入契约：暂无产物（roles/ 与 tests/fixtures/misc/ 都为空）→ 跳过")
        return

    checked: list[str] = []
    server, url = fake_llm.start_fake_mcp()
    try:
        if role_spec:
            config = make_config(url, tmp)
            config.extra["roles"] = {"misc": role_spec}
            platform = CtfyPlatform(url, config.token)
            platform.connect()
            store = Store(tmp / "misc_contract").load()
            store.challenges.clear()
            crew = Crew(config, platform, store, lambda model=None: fake_llm.ScriptedLLM([]))

            assert "misc" in crew.roles, sorted(crew.roles)
            enum = next(
                t for t in crew._coordinator_tools() if t["function"]["name"] == "delegate"
            )["function"]["parameters"]["properties"]["role"]["enum"]
            assert "misc" in enum, enum

            names = crew._role_platform_names("misc")
            assert names, "misc 角色没有拿到任何平台工具"
            assert names <= set(platform.tool_names()), names - set(platform.tool_names())
            if "submit" not in (role_spec.get("platform") or []):
                assert "submit_flag" not in names, names  # 专家不该有提交权

            prompt = Crew._role_system("misc", role_spec.get("system"))
            assert len(prompt) > 80, "misc 系统提示词太短（工单要求自带完整人设）"
            assert "report" in prompt, "提示词缺少 report 交付规则（框架应自动补 HANDOFF_RULE）"
            assert "evidence" in prompt.lower(), "提示词缺少 evidence 纪律（框架应自动补 EVIDENCE_RULE）"
            assert role_spec.get("allow_shell") is True, "misc 必须 allow_shell=true，否则 shell 全被拒"

            # 【防漂移】活配置 config/agent.json 的 misc 必须与 roles/misc.json 逐键一致。
            # 契约用例只验证 fragment，这一条把真跑会读的活配置也拉进回归网。
            live_misc = dict((load_config().extra.get("roles") or {}).get("misc") or {})
            assert live_misc == role_spec, (
                "config/agent.json 的 misc 与 roles/misc.json 漂移了：改了 config 必须同步 roles。"
                "差异键：" + str(sorted(set(live_misc) ^ set(role_spec)))
            )
            checked.append("roles")

        for name, spec in tools:
            function = spec.get("function") or {}
            assert function.get("name") == name, (name, function.get("name"))
            assert hasattr(LocalTools, name), f"SPEC 有 {name} 但没有同名方法（call() 按名字 getattr）"
            props = set((function.get("parameters") or {}).get("properties") or {})
            params = [
                p for p in inspect.signature(getattr(LocalTools, name)).parameters.values()
                if p.name != "self" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            ]
            assert {p.name for p in params} <= props, f"{name} 方法参数比 SPEC 多：{props}"

            local = LocalTools(tmp / "misc_tools", "misc-01", allow_shell=True)
            # 三处改动缺一不可：SPEC + specs() 注册 + 同名方法（call() 先用 names() 白名单拦，再 getattr）
            assert name in local.names(), (
                f"{name} 没挂进 LocalTools.specs()：call() 会直接报「未知本地工具」"
                "（工单 §3.2 要求改三处：SPEC、specs()、同名方法）"
            )
            clean = {
                p.name: ("" if "str" in str(p.annotation) else 1)
                for p in params
                if p.default is inspect.Parameter.empty
            }
            try:
                out = local.call(name, dict(clean))
            except ToolError as exc:
                if "未知本地工具" in str(exc):
                    raise AssertionError(f"{name} 未注册：{exc}") from exc
                out = ""  # 空参数下工具自己判非法输入、抛 ToolError 是允许的
            assert isinstance(out, str), f"{name} 必须返回 str"
            assert len(out) <= 6000, f"{name} 输出 {len(out)} 字符，超过 MAX_TOOL_TEXT=6000"

            # 未知参数必须被 call() 过滤掉（回显忽略提示），而不是 TypeError 崩掉整步
            try:
                local.call(name, {**clean, "__bogus__": 1})
            except TypeError as exc:
                raise AssertionError(f"{name} 对未知参数崩溃（call() 应过滤）：{exc}") from exc
            except ToolError:
                pass  # 未知参数被过滤后参数仍非法 → 工具按契约抛 ToolError，是允许的

            # 新工具也必须进账本：同参二次调用要被拦（工单 §3.4 第 2 条）
            ledger = Ledger()
            ledger.record(name, clean, out, role="misc")
            assert ledger.seen(name, clean) is not None, "新工具没进账本（同参二次调用拦不住）"
            checked.append(f"tool:{name}")

        for solve in fixtures:
            case_dir = solve.parent
            expected = case_dir / "expected.txt"
            assert expected.is_file(), f"{case_dir.name} 缺 expected.txt"
            flags = re.findall(r"[A-Za-z0-9_]{2,}\{[^}\s]{4,}\}", expected.read_text(encoding="utf-8"))
            assert flags, f"{case_dir.name}/expected.txt 里没有 FLAG{...} 形态"
            source = solve.read_text(encoding="utf-8")
            assert "expected.txt" not in source, f"{case_dir.name}/solve.py 直接读了 expected.txt（等于没验证）"
            proc = subprocess.run(
                [sys.executable, "solve.py"], cwd=str(case_dir),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            )
            assert proc.returncode == 0, f"{case_dir.name}/solve.py 退出码 {proc.returncode}: {proc.stderr[-300:]}"
            assert flags[0] in proc.stdout, f"{case_dir.name}: solve.py 没复现出 {flags[0]}"
            checked.append(f"fixture:{case_dir.name}")
    finally:
        server.shutdown()
        server.server_close()

    ok(f"外包接入契约：全部通过（{len(checked)} 项：{', '.join(checked[:6])}…）")


def case_web_playbook(tmp: Path) -> None:
    """web 角色提示词漂移护栏：三规则自动补上 + 关键条款 + 无触发字面。

    防的是「config/agent.json 的 roles.web.system 被悄悄改回/清空」这类静默回退：
    v3.x 正文刻意不含 PLAN/evidence/report 三个字面，一旦手改带进这些词，
    框架的 COMPANION/EVIDENCE/HANDOFF 规则就会被吞掉，49 项里没有别的用例能拦住。
    """
    from ctfy_agent.crew import Crew  # noqa: PLC0415

    web = dict((load_config().extra.get("roles") or {}).get("web") or {})
    base = web.get("system", "")
    assert base.strip(), "config/agent.json 的 roles.web.system 缺失或为空"

    # 关键条款（行为依赖项，缺了就是提示词回退）
    for needle in (
        "SPA 兜底", "不可信数据", "只有 10 步", "list_hints", "verify_answer",
        "你没有提交权", "ledger 拦截", "过滤矩阵", "回显三态", "command not found",
    ):
        assert needle in base, f"web 提示词缺关键条款: {needle}"

    # 禁止触发字面：一旦出现，框架三条规则会被吞掉
    assert "report" not in base, "web 提示词含 report 字面，会吞掉 HANDOFF_RULE"
    assert "evidence" not in base.lower(), "web 提示词含 evidence 字面，会吞掉 EVIDENCE_RULE"
    assert "PLAN" not in base, "web 提示词含 PLAN 字面，会吞掉 COMPANION_RULE"
    assert "[[" not in base, "web 提示词残留未替换占位符"
    assert "submit_answer" not in base, "web 角色不该提 submit_answer（无提交权）"

    # 三规则自动追加且各一次（单一来源，不重复）
    final = Crew._role_system("web", base)
    assert final.count("PLAN:") == 1, "COMPANION_RULE 未正确追加"
    assert final.count("结论纪律") == 1, "EVIDENCE_RULE 未正确追加"
    assert final.count("交付规则") == 1, "HANDOFF_RULE 未正确追加"

    ok("web 提示词护栏：关键条款 + 无触发字面 + 三规则各一次")


def case_a3_method_cards(tmp: Path) -> None:
    """A3 题型方法卡：实测键、表序确定、同卡去重、上限、快照漂移守卫、web 门控注入。

    守卫两件事：① tests/fixtures/web/tag_vocab.json（50 题实测词表快照）里每个 tag
    都必须被 CARDS 键覆盖或显式归类（aXX/AI 族/签到），防未来改表时静默漏键；
    ② 除签到题（000）与 AI 族（038-047，外包范围）外，每道 web 题至少命中一张卡。
    """
    from ctfy_agent.method_cards import (  # noqa: PLC0415
        CARDS, IGNORED_TAGS, MAX_CARDS, is_asi_tag, method_cards,
    )
    from ctfy_agent.state import Challenge  # noqa: PLC0415

    # 1) 035 真实 tags：关键卡命中 + 顺序只依赖表序 + 同卡多键只出一次
    tags_035 = ["owasp_2025_a07", "authentication_failures", "weak_password",
                "osint", "social_engineering", "express", "vue3"]
    cards = method_cards(tags_035)
    joined = "\n".join(cards)
    for needle in ("弱口令", "认证逻辑", "OSINT", "Node/Express", "SPA"):
        assert needle in joined, f"035 卡片缺：{needle}"
    assert cards == method_cards(list(reversed(tags_035))), "卡片顺序必须只依赖表序"
    assert sum("认证逻辑" in c for c in cards) == 1  # a07 与 authentication_failures 同卡

    # 2) 未命中 → 空（回退通用 playbook）；坏输入不炸
    assert method_cards(["not_a_real_tag"]) == []
    assert method_cards([]) == [] and method_cards(None) == []

    # 3) 攻击类在前、技术栈垫底；上限兜底
    two = method_cards(["vue3", "sqli"])
    assert "注入" in two[0] and "SPA" in two[1], two
    every = [k for keys, _ in CARDS for k in keys]
    assert len(method_cards(every)) <= MAX_CARDS

    # 4) 收割快照漂移守卫（见 docstring）
    vocab = json.loads((HERE / "fixtures" / "web" / "tag_vocab.json").read_text(encoding="utf-8"))
    mapped = {k for keys, _ in CARDS for k in keys}
    for tag in vocab["distinct_tags_list"]:
        assert (tag in mapped or tag in IGNORED_TAGS or tag.startswith("owasp_2025_a")
                or is_asi_tag(tag)), f"tag 未归类：{tag}"
    ai_covered = []
    for cid, tags in sorted(vocab["tags_by_challenge"].items()):
        if cid == "CTFARENA-000":
            continue  # 平台签到题，无攻击面
        cards = method_cards(tags)
        assert cards, f"{cid} 无卡片覆盖：{tags}"
        if any(is_asi_tag(t) for t in tags):
            ai_covered.append(cid)
            # M3：AI 族必须命中对应的 ASI 卡，且排在第一位（比叠加的 web 卡更具体）
            asi = next(t for t in tags if t.startswith("owasp_2026_asi"))[-2:]
            assert cards[0].startswith(f"ASI{asi}"), (cid, cards[0])
    assert len(ai_covered) == 10, f"AI 族应为 10 题（038-047）：{ai_covered}"
    # AI 卡优先：同时命中 ASI01 与 sqli 时，第一条必须是 AI 卡（键用实测的 owasp 前缀）
    assert method_cards(["owasp_2026_asi01", "sqli"])[0].startswith("ASI01")
    assert sum("ASI" in c for c in method_cards(["owasp_2026_asi01", "sqli"])) == 1

    # 5) 注入门控：web 两处注入、非 web 不注入、未知 tag 不注入
    store = Store(tmp / "a3_store").load()
    store.challenges.clear()
    item = Challenge(id="A3-W", category="web", tags=tags_035)
    assert "打法提示" in Solver._role_context(item)
    assert "打法提示" in Solver._initial_prompt(item, LocalTools(store, "web-01"))
    misc = Challenge(id="A3-M", category="misc", tags=tags_035)
    assert Solver._method_hint(misc) == "" and "打法提示" not in Solver._role_context(misc)
    unknown = Challenge(id="A3-U", category="web", tags=["not_a_real_tag"])
    assert Solver._method_hint(unknown) == ""

    ok("A3 题型方法卡：50 题快照全覆盖 + 表序/去重/上限 + web 门控注入两处")


def case_web_tool_whitelist(tmp: Path) -> None:
    """裁工具 28→13 的护栏：web 角色平台工具面 == 9 个白名单；删掉的不进 web、不丢出全局。

    证据来源：review-token-analysis.md —— 035 全程只调 http_request/report，15 个拟删工具零调用；
    web 系统提示词（[0 取材]）只点名白名单里的工具，删干净不会造成「提示词让调、工具面没有」。
    """
    from ctfy_agent.config import load
    from ctfy_agent.crew import DEFAULT_ROLES, PLATFORM_SCOPE, Crew

    keep = ["get_challenge", "list_challenge_attachments", "download_challenge_attachment",
            "list_hints", "view_hint", "start_instance",
            "list_instance_attachments", "download_instance_attachment", "verify_answer"]
    removed = ["get_me", "list_competitions", "get_competition", "list_competition_challenges",
               "search_challenges", "list_instances", "get_instance", "get_instance_status",
               "get_instance_traffic", "get_my_solves", "get_my_milestone_progress", "list_submissions",
               "get_scoreboard", "stop_instance", "renew_instance"]

    # 运行时白名单（config/agent.json 的 roles.web.platform）与代码兜底（DEFAULT_ROLES）必须一致
    web = dict((load().extra.get("roles") or {}).get("web") or {})
    assert web.get("platform") == keep, f"config roles.web.platform 白名单不对：{web.get('platform')}"
    assert DEFAULT_ROLES["web"]["platform"] == keep, "DEFAULT_ROLES.web.platform 与白名单不一致"

    # 删掉的 15 个仍留在全局 PLATFORM_SCOPE（协调者/别的角色要用），只是不进 web
    all_scoped = set().union(*PLATFORM_SCOPE.values())
    for t in removed:
        assert t in all_scoped, f"{t} 被误删出了 PLATFORM_SCOPE"
        assert t not in set(web["platform"]), f"{t} 不该还在 web 平台工具面"

    # 机制级：给定全 24 个工具名的 stub，web 解析结果恰好是 9 个
    class _Stub:
        @staticmethod
        def tool_names():
            return keep + removed

    crew = Crew.__new__(Crew)
    crew.roles = {"web": {"platform": keep}}
    crew.platform = _Stub()
    assert crew._role_platform_names("web") == set(keep)

    ok("裁工具护栏：web 平台工具面 == 9 白名单；删掉的 15 个既不进 web 也未丢出全局")


def case_competition_resolve(tmp: Path) -> None:
    """比赛 id 兜底解析：没有 config/meta 时按「题目属于哪场比赛」反查，而不是按标题猜。

    真机实测的坑：平台上练习场 CTFARENA 与正赛 TPCTF Agent Track 同时在线，
    标题含 TPCTF 的启发式会选到 upcoming 的正赛，导致 start_instance 报 competition_required。
    """
    from ctfy_agent.config import load
    from ctfy_agent.solver import Solver
    from ctfy_agent.state import Store

    class _CompPlatform:
        def __init__(self):
            self.asked: list[str] = []

        def list_competitions(self):
            return [
                {"id": "comp-contest", "title": "TPCTF 2026 · Agent Track", "status": "published"},
                {"id": "comp-practice", "title": "CTFARENA", "status": "published"},
            ]

        def list_challenges(self, competition_id=None, **_):
            self.asked.append(competition_id)
            return {
                "comp-contest": [{"id": "TPCTF-001"}],
                "comp-practice": [{"id": "CTFARENA-006"}, {"id": "CTFARENA-007"}],
            }.get(competition_id, [])

    config = load()
    config.extra.pop("competition_id", None)
    store = Store(tmp / "compresolve").load()
    store.meta.pop("competition_id", None)

    solver = Solver.__new__(Solver)
    solver.config = config
    solver.store = store
    solver.platform = _CompPlatform()

    # 反查命中练习场（不是标题更像正赛的那一场）
    assert solver._competition_id("CTFARENA-006") == "comp-practice"
    assert store.meta.get("competition_id") == "comp-practice", "反查结果没写回 meta"

    # 缓存后不再问平台
    solver.platform.asked.clear()
    assert solver._competition_id("CTFARENA-007") == "comp-practice"
    assert solver.platform.asked == [], solver.platform.asked

    # 题目不在任何比赛里 → 不猜
    store.meta.pop("competition_id", None)
    assert solver._competition_id("NOPE-999") is None

    # 显式配置永远优先
    config.extra["competition_id"] = "comp-contest"
    assert solver._competition_id("CTFARENA-006") == "comp-contest"
    config.extra.pop("competition_id", None)

    # 没有 challenge_id 时不去扫平台（避免无谓 MCP 调用）
    store.meta.pop("competition_id", None)
    solver.platform.asked.clear()
    assert solver._competition_id() is None
    assert solver.platform.asked == []

    ok("比赛 id 兜底：按 membership 反查（非标题启发式）+ 命中后缓存 + 配置优先")


def case_competition_mismatch_repair(tmp: Path) -> None:
    """A 附带项：陈旧 meta 把题塞错场次 → start_instance 报 challenge_not_found → 清缓存反查重试成功。"""
    from ctfy_agent.solver import Solver
    from ctfy_agent.state import Store

    class _StaleMetaPlatform:
        def __init__(self):
            self.start_calls: list = []

        def list_competitions(self):
            return [{"id": "comp-right", "title": "Right", "status": "running"}]

        def list_challenges(self, competition_id=None, **_):
            return [{"id": "X-1"}] if competition_id == "comp-right" else []

        def start_instance(self, challenge_id, competition_id=None):
            self.start_calls.append(competition_id)
            if competition_id == "stale-comp":
                return "Error calling tool 'start_instance': challenge_not_found: Challenge X-1 not found"
            if competition_id == "comp-right":
                return {"id": "inst-1", "host": "h", "port": 1}
            return f"unexpected competition_id={competition_id}"

    platform = _StaleMetaPlatform()
    config = make_config("http://127.0.0.1:1", tmp)
    store = Store(tmp / "mismatch").load()
    store.meta["competition_id"] = "stale-comp"
    store.save()

    solver = Solver.__new__(Solver)
    solver.config = config
    solver.store = store
    solver.platform = platform
    solver.on_event = lambda kind, payload=None: None

    info = solver._start_with_backoff("X-1")
    assert info == {"id": "inst-1", "host": "h", "port": 1}, info
    # 第一次用陈旧 id 撞墙，第二次反查到正确场次
    assert platform.start_calls == ["stale-comp", "comp-right"], platform.start_calls
    ok("陈旧 meta 触发 challenge_not_found → 清缓存反查重试，实例照常拉起")


def case_checkpoint(tmp: Path) -> None:
    """P0-1 断点续跑：协调者状态的 checkpoint 存/取往返 + 防御性读回。"""
    from ctfy_agent.checkpoint import (  # noqa: PLC0415
        discard as ckpt_discard,
        load as ckpt_load,
        save as ckpt_save,
        ledger_from_dict,
        plan_from_dict,
    )
    from ctfy_agent.companion import PlanTrack  # noqa: PLC0415
    from ctfy_agent.ledger import Ledger  # noqa: PLC0415

    # 1) 造一个「跑了一半」的协调者状态
    ledger = Ledger(refute_after=2)
    ledger.record("http_request", {"url": "http://x/api", "method": "GET"}, '{"status": 200}', role="web")
    ledger.record("shell", {"command": "cat flag"}, "ctf{xx}", role="rev")
    ledger.record("delegate", {"role": "web", "objective": "读 /api"}, "报告", role="coordinator", ok=True)
    ledger.note_duplicate(ledger.order[0].key)  # 触发 duplicates 计数

    plan = PlanTrack(stall_after=3, max_reminders=2)
    plan.observe("PLAN: 先读 JS\nREFLECTION: 探路", progressed=True)
    for _ in range(3):  # 连续三步没推进 → stall_rounds 累计到 3，触发停滞
        plan.observe("PLAN: 先读 JS\nREFLECTION: 无进展", progressed=False)

    messages = [
        {"role": "system", "content": "coord"},
        {"role": "user", "content": "brief"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "delegate", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "报告"},
    ]
    reports = ["[web 结论]\n已定位 /api"]
    path = tmp / "checkpoints" / "web-01.json"

    ckpt_save(path, challenge_id="web-01", next_round=3, messages=messages, reports=reports, ledger=ledger, plan=plan)
    assert path.is_file()

    # 2) 读回并逐项校验
    data = ckpt_load(path, challenge_id="web-01")
    assert data is not None
    assert data["next_round"] == 3
    assert data["messages"] == messages
    assert data["reports"] == reports

    led2 = ledger_from_dict(data["ledger"])
    assert led2 is not None
    assert len(led2.order) == 3
    assert led2.duplicates == 1
    assert led2._seq == 3
    assert led2.seen("shell", {"command": "cat flag"}) is not None  # 去重键恢复

    plan2 = plan_from_dict(data["plan"])
    assert plan2 is not None
    assert plan2.plan == "先读 JS"
    assert plan2.plan_changes == 1
    assert plan2.stall_rounds == 3
    assert plan2.stalled() is True

    # 3) 防御性读回：题号不符 / 缺文件 / 损坏 → None（绝不抛）
    assert ckpt_load(path, challenge_id="other") is None
    assert ckpt_load(tmp / "checkpoints" / "nope.json", challenge_id="web-01") is None
    bad = tmp / "checkpoints" / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ckpt_load(bad, challenge_id="web-01") is None

    # 3b) P2-1 回归：JSON 合法但字段类型/版本不对 → 同样返回 None（绝不把坏数据带进恢复块）
    base = {"version": 1, "challenge_id": "web-01", "next_round": 1, "messages": [], "reports": []}
    for name, mutate in (
        ("bad_version", lambda d: d.update(version=999)),
        ("bad_rnd", lambda d: d.update(next_round="abc")),
        ("bad_msgs", lambda d: d.update(messages="not-a-list")),
        ("bad_reports", lambda d: d.update(reports={"a": 1})),
    ):
        data = dict(base)
        mutate(data)
        (tmp / "checkpoints" / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        assert ckpt_load(tmp / "checkpoints" / f"{name}.json", challenge_id="web-01") is None, name
    # from_dict 直接喂非 dict 也不崩（AttributeError 曾从这里上抛）
    assert ledger_from_dict([1, 2, 3]) is None
    assert ledger_from_dict("abc") is None
    assert plan_from_dict("abc") is None
    assert plan_from_dict([1, 2]) is None

    # 4) 正常收尾删除
    ckpt_discard(path)
    assert not path.is_file()
    ok("断点续跑：checkpoint 存/取往返 + 账本/计划状态机还原 + 防御性读回")


def case_checkpoint_resume(tmp: Path) -> None:
    """P3-2 回归：crew.solve 真的从 checkpoint 续跑——恢复上下文、从第 N 轮继续，不重跑已完成的轮。"""
    from ctfy_agent.checkpoint import save as ckpt_save  # noqa: PLC0415
    from ctfy_agent.companion import PlanTrack  # noqa: PLC0415
    from ctfy_agent.crew import Crew  # noqa: PLC0415
    from ctfy_agent.ledger import Ledger  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "resume").load()
        store.challenges.clear()
        seed_challenge(store)
        # 模拟「跑完第 1 轮后进程被杀」：状态仍在 probing，checkpoint 已落盘
        store.require("web-01").status = "probing"
        store.save()

        restored = [
            {"role": "system", "content": "RESTORED-SYSTEM"},
            {"role": "user", "content": "RESTORED-BRIEF"},
            {"role": "assistant", "content": "RESTORED-ASSISTANT", "tool_calls": None},
            {"role": "tool", "tool_call_id": "c1", "content": "RESTORED-TOOL-RESULT"},
        ]
        ckpt_path = store.run_dir / "checkpoints" / (store.transcript_path("web-01").stem + ".json")
        ckpt_save(ckpt_path, challenge_id="web-01", next_round=2, messages=restored,
                  reports=["[web 结论]\nrestored"], ledger=Ledger(), plan=PlanTrack())

        coordinator = fake_llm.ScriptedLLM(
            [{"content": "收工", "tool_calls": [tool_call("done", {})]}]
        )

        def factory(model=None):
            return coordinator

        crew = Crew(config, platform, store, factory)
        summary = crew.solve("web-01")

        assert summary["reason"] == "coordinator_done", summary
        # 只跑了一轮（第 2 轮），没从第 1 轮重跑
        assert coordinator.calls == 1, coordinator.calls
        # 协调者收到的首条消息是 checkpoint 里恢复的，而不是默认 COORDINATOR_PROMPT
        assert coordinator.received[0][0]["content"] == "RESTORED-SYSTEM", coordinator.received[0][0]
        assert coordinator.received[0][1]["content"] == "RESTORED-BRIEF"
        # 转录里留了「断点续跑」锚点
        recs = [
            json.loads(ln)
            for ln in store.transcript_path("web-01").read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        ckpt_notes = [r for r in recs if r.get("role") == "checkpoint" and "断点续跑" in str(r.get("note", ""))]
        assert ckpt_notes, "转录里没有断点续跑锚点"
        ok("断点续跑集成：crew.solve 恢复 checkpoint 上下文并从第 2 轮继续，不重跑第 1 轮")
    finally:
        server.shutdown()
        server.server_close()


def case_coordinator_digest(tmp: Path) -> None:
    """P0-2 回归：协调者每轮派活后收到「已试过」账本摘要（含专家实际探测的参数），防止换说法重派。"""
    from ctfy_agent.crew import Crew  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        config.extra["roles"] = {
            "web": {
                "max_steps": 4,
                "platform": ["read", "instance", "oracle"],
                "local": ["http_request"],
                "model": "web-script",
            },
        }
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "digest").load()
        store.challenges.clear()
        seed_challenge(store)

        coordinator = fake_llm.ScriptedLLM(
            [
                {
                    "content": "PLAN: 派 web 读 /api\nREFLECTION: -\n",
                    "tool_calls": [tool_call("delegate", {"role": "web", "objective": "读 /api"})],
                },
                {"content": "PLAN: 收工\nREFLECTION: -\n", "tool_calls": [tool_call("done", {})]},
            ]
        )
        web = fake_llm.ScriptedLLM(
            [
                {"content": "", "tool_calls": [tool_call("get_challenge", {"challenge_id": "web-01"})]},
                {
                    "content": "",
                    "tool_calls": [
                        tool_call("report", {"text": "题面是 Login Bypass", "evidence": '"name": "Login Bypass"'})
                    ],
                },
            ]
        )

        def factory(model=None):
            return web if model == "web-script" else coordinator

        crew = Crew(config, platform, store, factory)
        crew.solve("web-01")

        recs = [
            json.loads(ln)
            for ln in store.transcript_path("web-01").read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        digest_recs = [
            r for r in recs
            if r.get("role") == "ledger" and "已试过（确定性账本" in str(r.get("content", ""))
        ]
        assert digest_recs, "转录里没有协调者「已试过」摘要注入"
        # 摘要也进了协调者第二轮上下文（消息级，而非只落转录）
        assert len(coordinator.received) >= 2, coordinator.received
        assert any(
            "已试过（确定性账本" in str(m.get("content", "")) for m in coordinator.received[1]
        ), "第二轮上下文里没有账本摘要"
        ok("P0-2：协调者每轮派活后收到「已试过」账本摘要（进上下文 + 落转录）")
    finally:
        server.shutdown()
        server.server_close()


def case_bench_stats(tmp: Path) -> None:
    """P2：离线统计工具从 run_dir 读出 状态/轮数/调用/token/峰值/救援/续跑/账本重复拦截。"""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runs"))
    from bench_stats import analyze_run  # noqa: PLC0415

    run_dir = tmp / "bench"
    (run_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {"challenges": {"web-01": {"status": "submitted", "reason": "flag_accepted"}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    recs = [
        {"role": "coordinator", "step": 1, "tool": "delegate",
         "args": {"role": "web", "objective": "读 /api"}, "result": "[web 结论]\n结论",
         "usage": {"prompt_tokens": 5000, "completion_tokens": 60}},
        {"role": "web", "step": 1, "tool": "http_request",
         "args": {"url": "http://x/api", "method": "GET"}, "result": '{"status": 200}',
         "usage": {"prompt_tokens": 4000, "completion_tokens": 20}},
        {"role": "web", "step": "rescue", "result": "卡在登录"},
        {"role": "checkpoint", "note": "断点续跑：从第 2 轮继续"},
        {"role": "coordinator", "step": 2, "tool": "submit",
         "args": {"answer": "ctf{xx}"}, "result": "accepted",
         "usage": {"prompt_tokens": 6000, "completion_tokens": 30}},
        # 与第一条 http_request 完全相同的重复调用 → 账本应拦截 1 次
        {"role": "web", "step": 2, "tool": "http_request",
         "args": {"url": "http://x/api", "method": "GET"}, "result": '{"status": 200}'},
    ]
    (run_dir / "transcripts" / "web-01.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8"
    )

    rows = analyze_run(run_dir)
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["challenge_id"] == "web-01"
    assert r["status"] == "submitted"
    assert r["rounds"] == 2, r
    assert r["llm_calls"] == 3, r                       # 3 条带 usage 的记录
    assert r["prompt_tokens"] == 5000 + 4000 + 6000, r
    assert r["peak_prompt"] == 6000, r
    assert r["rescue"] == 1, r
    assert r["resume"] == 1, r
    assert r["duplicates"] == 1, r                      # 第二条 http_request 与第一条相同
    ok("P2 bench_stats：离线读出 状态/轮数/调用/token/峰值/救援/续跑/账本重复拦截")


# ---------- B1 容器适配层 ----------

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fresh_dir(path: Path) -> Path:
    """清空并重建目录——store/transcript 跨 smoke.py 次运行残留会让提交命中幂等拦截。"""
    import shutil

    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_result(result_path: Path) -> dict:
    """result.json 三不变量：普通文件（非符号链接）、≤4MiB 合法 JSON、无 .tmp 残留。"""
    assert result_path.is_file(), "result.json 不是普通文件"
    assert not result_path.is_symlink(), "result.json 是符号链接（O_NOFOLLOW 会拒读）"
    text = result_path.read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= 4 * 1024 * 1024, "result.json 超 4MiB"
    rollup = json.loads(text)
    assert rollup["schema_version"] == 2, rollup
    assert not list(result_path.parent.glob(result_path.name + ".tmp.*")), "残留 .tmp 临时文件"
    return rollup


def case_agent_e2e(tmp: Path) -> None:
    from ctfy_agent import agent_main  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        platform = CtfyPlatform(url, "pf_test_token_123456")
        platform.connect()
        coordinator = fake_llm.ScriptedLLM(
            [
                {"content": "直接提交", "tool_calls": [tool_call("submit", {"answer": fake.ACCEPT_FLAG})]},
                {"content": "收工", "tool_calls": [tool_call("done", {})]},
            ]
        )
        work = _fresh_dir(tmp / "agent_e2e")
        task_path = work / "task.json"
        task_path.write_text((FIXTURES / "task_instance.json").read_text(encoding="utf-8"), encoding="utf-8")
        result_path = work / "result.json"
        env = {
            "LLM_FLAVOR": "openai",
            "CTFY_TOKEN": "pf_test_token_123456",
            "LLM_API_KEY": "sk-test",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_MODEL": "test-model",
            "CTFY_EVAL_CHALLENGE_ID": "web-01",
        }
        code = agent_main.run_agent(task_path=task_path, work_dir=work, env=env,
                                    platform=platform, llm_factory=lambda model=None: coordinator,
                                    result_path=result_path)
        assert code == 0, code
        rollup = _read_result(result_path)
        assert rollup["challenge_id"] == "web-01", rollup
        assert rollup["stop_reason"] == "done", rollup
        assert rollup["solved"] == ["flag"], rollup       # 审计 C1：solved 是题目 id 列表
        assert rollup["answers"] == {"flag": fake.ACCEPT_FLAG}, rollup
        assert rollup["fully_solved"] is True, rollup
        assert rollup["turns"] >= 1, rollup
        assert rollup["tool_calls_made"] >= 1, rollup
        # F3：最终结果落盘后信号处理器不再覆盖（_RESULT_PATH 已清空）
        assert agent_main._RESULT_PATH is None
        # F1：题型线索已从平台 get_challenge 入库（A3 方法卡 / brief 题型行依赖它）
        facts = Store(work / "runs" / "web-01").load().require("web-01")
        assert set(facts.tags) == {"authentication_failures", "weak_password", "osint"}, facts.tags
        assert facts.difficulty == "easy", facts.difficulty
        assert facts.solves_count == 10, facts.solves_count
        ok("B1 agent：task.json → Crew → result.json 全链路（solved 列表 / answers 交接 / 题型入库 / exit 0）")
    finally:
        server.shutdown()
        server.server_close()


def case_agent_dry_run(tmp: Path) -> None:
    from ctfy_agent.agent_main import run_agent  # noqa: PLC0415

    work = _fresh_dir(tmp / "agent_dry")
    task_path = work / "task.json"
    task_path.write_text((FIXTURES / "task_dry_run.json").read_text(encoding="utf-8"), encoding="utf-8")
    result_path = work / "result.json"
    # 双信号：env CTFY_EVAL_DRY_RUN=1 + task.dry_run:true；不给 token/key，也不注入平台
    code = run_agent(task_path=task_path, work_dir=work, env={"CTFY_EVAL_DRY_RUN": "1"},
                     result_path=result_path)
    assert code == 0, code
    rollup = _read_result(result_path)
    assert rollup["stop_reason"] == "dry_run", rollup
    assert rollup["solved"] == [] and rollup["fully_solved"] is False, rollup
    ok("B1 agent：dry run 双信号 → 最便宜 rollup，不碰网络/凭证")


def case_agent_degraded(tmp: Path) -> None:
    from ctfy_agent.agent_main import run_agent  # noqa: PLC0415

    base_env = {
        "LLM_FLAVOR": "openai",
        "CTFY_TOKEN": "pf_test",
        "LLM_API_KEY": "sk-test",
        "LLM_BASE_URL": "https://llm.example/v1",
        "LLM_MODEL": "m",
    }
    # (fixture 名, env_extra, 期望 stop_reason, writeup 关键词)
    scenarios = [
        ("task_qa.json", {}, "refused", "run_mode"),     # 非 instance 模式
        (None, {}, "halted", "task.json"),               # task.json 缺失
        ("task_broken.json", {}, "halted", "task.json"),  # task.json 坏 JSON
        ("task_instance.json", {"LLM_FLAVOR": "anthropic"}, "refused", "anthropic"),
        ("task_instance.json", {"LLM_API_KEY": ""}, "refused", "CTFY_TOKEN"),
    ]
    for idx, (fixture, env_extra, expect, note_frag) in enumerate(scenarios):
        work = _fresh_dir(tmp / f"agent_degraded_{idx}")
        task_path = work / "task.json"
        if fixture:
            task_path.write_text((FIXTURES / fixture).read_text(encoding="utf-8"), encoding="utf-8")
        result_path = work / "result.json"
        code = run_agent(task_path=task_path, work_dir=work, env={**base_env, **env_extra},
                         result_path=result_path)
        assert code == 0, (idx, code)
        rollup = _read_result(result_path)
        assert rollup["stop_reason"] == expect, (idx, rollup)
        assert note_frag in rollup["writeup"], (idx, rollup)
    ok("B1 agent：qa 模式 / 缺任务 / 坏任务 / 非 openai flavor / 缺凭证 全优雅降级 + exit 0")


def case_agent_exception_safe(tmp: Path) -> None:
    from ctfy_agent.agent_main import run_agent  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        platform = CtfyPlatform(url, "pf_test_token_123456")
        platform.connect()

        class Boom:
            def chat(self, *a, **k):
                raise RuntimeError("boom")

        work = _fresh_dir(tmp / "agent_exc")
        task_path = work / "task.json"
        task_path.write_text((FIXTURES / "task_instance.json").read_text(encoding="utf-8"), encoding="utf-8")
        env = {
            "LLM_FLAVOR": "openai",
            "CTFY_TOKEN": "pf_test_token_123456",
            "LLM_API_KEY": "sk-test",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_MODEL": "m",
        }
        result_path = work / "result.json"
        code = run_agent(task_path=task_path, work_dir=work, env=env,
                         platform=platform, llm_factory=lambda model=None: Boom(),
                         result_path=result_path)
        assert code == 0, code
        rollup = _read_result(result_path)
        assert rollup["stop_reason"] == "halted", rollup
        assert "boom" in rollup["writeup"], rollup
        ok("B1 agent：解题异常也不毁 result.json（顶层兜底仍 exit 0）")
    finally:
        server.shutdown()
        server.server_close()


def case_agent_seed(tmp: Path) -> None:
    from ctfy_agent.agent_main import _build_config, _seed_challenge  # noqa: PLC0415

    task = json.loads((FIXTURES / "task_gated.json").read_text(encoding="utf-8"))
    env = {"CTFY_TOKEN": "pf_test", "LLM_FLAVOR": "openai", "LLM_API_KEY": "sk-test",
           "LLM_BASE_URL": "https://llm.example/v1", "LLM_MODEL": "m"}
    config = _build_config(task, tmp, "http://x/v1/mcp/", env)
    # G6：非 "flag" 的题目 id 要传给 _submit 的默认值；F2：submit_limit 计入门控题
    assert config.extra.get("default_question_id") == "q1", config.extra
    assert config.submit_limit == 6, config.submit_limit          # (1 明题 + 2 门控) * 2
    # 归一化：角色 model 被剥掉（网关会改），max_tokens 统一到容器默认
    for spec in (config.extra.get("roles") or {}).values():
        if isinstance(spec, dict):
            assert "model" not in spec, spec
            assert spec.get("max_tokens") == config.max_tokens, spec

    # F8：无预算（max_turns=null）时 AGENT_MAX_TURNS 是逃生阀
    env55 = dict(env, AGENT_MAX_TURNS="55")
    config55 = _build_config({"questions": [], "max_turns": None}, tmp, "http://x/v1/mcp/", env55)
    assert config55.max_llm_calls == 55, config55.max_llm_calls

    store = Store(tmp / "agent_seed_store").load()
    cid = _seed_challenge(store, task, {"CTFY_EVAL_CHALLENGE_ID": ""})
    assert cid == "gated-01", cid
    desc = store.require(cid).description
    assert "问题 q1" in desc, desc
    assert "submit 时 question_id 用 `q1`" in desc, desc
    assert "门控题" in desc and "2 道" in desc, desc
    ok("B1 agent：门控题提示 + 非 flag 题目 id 默认值 + 角色归一化")


def case_agent_cli_entry(tmp: Path) -> None:
    from ctfy_agent.agent_main import main as agent_main  # noqa: PLC0415

    work = _fresh_dir(tmp / "agent_cli_entry")
    task_path = work / "task.json"
    # 本地直跑：不带 result.path（否则按规范 result.path 会赢，结果写到 /ctfy/work），
    # 默认 result.json 落 task 同目录，好验证 --task → work_dir 推导。
    task_path.write_text(
        json.dumps({"schema_version": 2, "run_mode": "instance", "dry_run": True,
                    "challenge": {"id": "web-01"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    old = os.environ.get("CTFY_WORK_DIR")
    os.environ.pop("CTFY_WORK_DIR", None)
    try:
        code = agent_main(["--task", str(task_path)])
    finally:
        if old is None:
            os.environ.pop("CTFY_WORK_DIR", None)
        else:
            os.environ["CTFY_WORK_DIR"] = old

    assert code == 0, code
    rollup = _read_result(work / "result.json")
    assert rollup["stop_reason"] == "dry_run", rollup
    assert not (FIXTURES / "result.json").exists(), "污染了 tests/fixtures/"
    ok("B1 agent：CLI 入口（main --task → work_dir 推导 → 写盘位置）")


def case_agent_mcp_url(tmp: Path) -> None:
    from ctfy_agent.agent_main import _resolve_mcp_url  # noqa: PLC0415

    work = _fresh_dir(tmp / "agent_mcp_url")
    (work / "mcp.json").write_text(
        json.dumps({"mcpServers": {"ctfy": {"url": "https://ctfy.example/api/v1/mcp/?token=${CTFY_TOKEN}"}}},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    env = {"CTFY_TOKEN": "pf_test_token_123456"}
    assert _resolve_mcp_url(work, "", env) == "https://ctfy.example/api/v1/mcp/?token=pf_test_token_123456"

    work2 = _fresh_dir(tmp / "agent_mcp_url2")
    assert _resolve_mcp_url(work2, "https://ctfy.example", {}) == "https://ctfy.example/api/v1/mcp/"
    assert _resolve_mcp_url(work2, "", {}) is None
    ok("B1 agent：MCP 端点解析（mcp.json ${VAR} 展开 + platform_url 兜底）")


def case_crew_step_limit(tmp: Path) -> None:
    from ctfy_agent.crew import Crew  # noqa: PLC0415

    server, url = fake_llm.start_fake_mcp()
    try:
        config = make_config(url, tmp)
        config.extra["roles"] = {"coordinator": {"max_steps": 3}}
        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(tmp / "crew_step_limit").load()
        store.challenges.clear()
        seed_challenge(store)
        # 协调者每轮只 note（不提交、不收工），把 3 轮烧完 → 应落到 step_limit
        coordinator = fake_llm.ScriptedLLM(
            [{"content": "", "tool_calls": [tool_call("note", {"text": f"第{i}轮"})]} for i in range(5)]
        )
        summary = Crew(config, platform, store, lambda model=None: coordinator).solve("web-01")
        assert summary["reason"] == "step_limit", summary
        assert summary["submitted"] is False
        ok("B1/F5：协调者轮数耗尽 → reason=step_limit（区别于 no_more_actions）")
    finally:
        server.shutdown()
        server.server_close()


def case_jwt_forge(tmp: Path) -> None:
    """jwt_forge：none / HS256 / RS256+jwk / 改写+remove，且 secret 不进账本明文。"""
    import base64 as b64  # noqa: PLC0415
    import hashlib  # noqa: PLC0415
    import hmac  # noqa: PLC0415

    from ctfy_agent import tools_misc_ext as tme  # noqa: PLC0415
    from ctfy_agent.ledger import attempt_key, attempt_label  # noqa: PLC0415

    def u(raw: bytes) -> str:
        return b64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def ud(text: str) -> bytes:
        return b64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    store = Store(tmp / "jwt").load()
    store.challenges.clear()
    seed_challenge(store)
    local = LocalTools(store, "web-01")

    # none：三段且签名段为空
    out = json.loads(local.call("jwt_forge", {"alg": "none", "claims": {"sub": "admin"}}))
    assert out["header"]["alg"] == "none" and out["token"].split(".")[2] == ""

    # HS256 往返 + secret 不回显
    out = json.loads(local.call("jwt_forge", {"alg": "HS256", "secret": "k3y", "claims": {"role": "admin"}}))
    h, p, sig = out["token"].split(".")
    assert sig == u(hmac.new(b"k3y", (h + "." + p).encode(), hashlib.sha256).digest())
    assert "k3y" not in json.dumps(out) and out["secret_len"] == 3

    # RS256：jwk 注入 + 用公钥验 PKCS#1 v1.5 结构
    out = json.loads(local.call("jwt_forge", {"alg": "RS256", "claims": {"sub": "root"}}))
    assert out["header"]["jwk"]["kty"] == "RSA" and out["header"]["jwk"]["alg"] == "RS256"
    sig_int = int.from_bytes(ud(out["token"].split(".")[2]), "big")
    k = (tme._RSA_N.bit_length() + 7) // 8
    em = pow(sig_int, tme._RSA_E, tme._RSA_N).to_bytes(k, "big")
    assert em[:2] == b"\x00\x01" and tme._SHA256_DI in em

    # token 基底改写 + remove
    base = json.loads(local.call("jwt_forge", {"alg": "HS256", "secret": "k", "claims": {"sub": "a", "exp": 1}}))
    out = json.loads(local.call("jwt_forge", {"token": base["token"], "alg": "HS256", "secret": "k",
                                              "claims": {"role": "admin"}, "remove": ["exp"]}))
    assert out["claims"]["role"] == "admin" and out["claims"]["sub"] == "a" and "exp" not in out["claims"]

    # 账本：secret/token 只留 sha1 前 8 位
    key = attempt_key("jwt_forge", {"alg": "HS256", "secret": "S3cret!", "claims": {"sub": "x"}})
    label = attempt_label("jwt_forge", {"alg": "HS256", "secret": "S3cret!", "claims": {"sub": "x"}})
    assert "S3cret!" not in key and "S3cret!" not in label
    ok("jwt_forge：none/HS256/RS256+jwk/改写+remove + secret 不进账本明文")


def case_http_p0(tmp: Path) -> None:
    """http_request P0：elapsed_ms / OPTIONS / follow_redirect=false / body_b64。"""
    import base64 as b64  # noqa: PLC0415

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/landing")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"hello")

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"options-ok")

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(raw.hex().encode())  # 回显十六进制，断言确定

        def log_message(self, *a):  # noqa: N802
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        store = Store(tmp / "p0").load()
        store.challenges.clear()
        seed_challenge(store)
        local = LocalTools(store, "web-01")
        local.allow_instance("127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"

        out = json.loads(local.http_request(base + "/"))
        assert "elapsed_ms" in out and out["elapsed_ms"] >= 0, out

        out = json.loads(local.http_request(base + "/opt", method="OPTIONS"))
        assert out["status"] == 200 and "options-ok" in out["body"], out

        # BUG-2 回归：先默认跟随（缓存 /redirect 的落地页 200），再 follow_redirect=false
        # 必须绕过缓存看到原始 302，而不是吃到缓存的落地页。
        followed = json.loads(local.http_request(base + "/redirect"))
        assert followed["status"] == 200, followed
        out = json.loads(local.http_request(base + "/redirect", follow_redirect=False))
        assert out["status"] == 302 and out["headers"].get("Location") == "/landing", out

        blob = b"\x00\xff\x10hello\x00"
        out = json.loads(local.http_request(base + "/", method="POST", body_b64=b64.b64encode(blob).decode()))
        assert blob.hex() in out["body"], out

        try:
            local.http_request(base + "/", method="POST", body="x", body_b64="eA==")
        except ToolError:
            pass
        else:
            raise AssertionError("body 与 body_b64 互斥未拦截")
        ok("http P0：elapsed_ms / OPTIONS / follow_redirect=false(302+Location) / body_b64 + 互斥拦截")
    finally:
        server.shutdown()
        server.server_close()


def case_http_burst(tmp: Path) -> None:
    """http_burst：race 竞态分组 + batch 枚举（url/body 的 {v} 替换）+ count 钳制。"""
    hits: list[int] = []
    lock = threading.Lock()
    state = [0]   # 有状态计数：GET /state 读、POST /bump 加一（测 burst 后缓存是否失效）

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/race":
                with lock:
                    hits.append(1)
                    n = len(hits)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"TARGET_REACHED" if n == 1 else b"MISS")
            elif self.path.startswith("/enum/"):
                v = self.path.split("/", 2)[2]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(f"user={v}".encode())
            elif self.path == "/state":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(str(state[0]).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):  # noqa: N802
            if self.path == "/bump":
                state[0] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"echo={raw}".encode())

        def log_message(self, *a):  # noqa: N802
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        store = Store(tmp / "burst").load()
        store.challenges.clear()
        seed_challenge(store)
        local = LocalTools(store, "web-01")
        local.allow_instance("127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"

        out = json.loads(local.http_burst(url=base + "/race", method="GET", count=8))
        assert out["mode"] == "race" and out["requested"] == 8 and out["ok"] == 8, out
        assert out["distinct_bodies"] == 2 and out["groups"][0]["count"] == 7, out
        assert "ms" in out and out["ms"]["max"] >= out["ms"]["min"] >= 0, out

        out = json.loads(local.http_burst(url=base + "/enum/{v}", method="GET", values=["alice", "bob", "carol"]))
        assert out["mode"] == "batch" and out["distinct_bodies"] == 3, out
        assert sorted(g["v"][0] for g in out["groups"]) == ["alice", "bob", "carol"], out

        out = json.loads(local.http_burst(url=base + "/submit", method="POST", body='{"id":"{v}"}', values=["1", "2"]))
        bodies = sorted(g["head"] for g in out["groups"])
        assert bodies == ['echo={"id":"1"}', 'echo={"id":"2"}'], bodies

        out = json.loads(local.http_burst(url=base + "/race", method="GET", count=99))
        assert out["requested"] == 24, out

        # BUG-1 回归：burst 的非 GET 必须失效同 host 缓存，否则后续 GET 读到 burst 之前的旧值
        first = json.loads(local.http_request(base + "/state"))
        assert first["body"] == "0", first
        json.loads(local.http_burst(url=base + "/bump", method="POST", count=2))
        after = json.loads(local.http_request(base + "/state"))
        assert after["body"] == "2", after

        ok("http_burst：race 竞态分组 + batch URL/body 替换 + count 钳制 + 缓存失效")
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    tmp = Path(os.environ.get("TEMP", ".")) / "ctfy_agent_smoke"
    tmp.mkdir(parents=True, exist_ok=True)

    cases = [
        ("protocol", case_protocol),
        ("auth", case_auth),
        ("resolution", case_resolution),
        ("solver", case_solver),
        ("submit_limit", case_submit_limit),
        ("budget_limits", case_budget_limits),
        ("budget_gate", case_budget_gate),
        ("crew", case_crew),
        ("role_extension", case_role_extension),
        ("role_extension_guard", case_role_extension_guard),
        ("auto_batch", case_auto_batch),
        ("instance_release", case_instance_release),
        ("shell_python", case_shell_python),
        ("guard", case_guard),
        ("hardening", case_hardening),
        ("compact", case_compact),
        ("state_machine", case_state_machine),
        ("role_deep_merge", case_role_deep_merge),
        ("submit_name", case_submit_name),
        ("release_on_submit", case_release_on_submit),
        ("http_offset_cache", case_http_offset_cache),
        ("http_cookie_jar", case_http_cookie_jar),
        ("http_diff", case_http_diff),
        ("run_role_synthesis", case_run_role_synthesis),
        ("rescue_report", case_rescue_report),
        ("ensure_instance", case_ensure_instance),
        ("windows_shell_guard", case_windows_shell_guard),
        ("prompt_hints", case_prompt_hints),
        ("limit_and_kwargs", case_limit_and_kwargs),
        ("prior_digest", case_prior_digest),
        ("challenge_meta", case_challenge_meta),
        ("multi_question", case_multi_question),
        ("stdlib_only", case_stdlib_only),
        ("hint_scope", case_hint_scope),
        ("ledger", case_ledger),
        ("ledger_crew", case_ledger_crew),
        ("report_evidence", case_report_evidence),
        ("plan_companion", case_plan_companion),
        ("delegate_objective", case_delegate_objective),
        ("web_playbook", case_web_playbook),
        ("a3_method_cards", case_a3_method_cards),
        ("web_tool_whitelist", case_web_tool_whitelist),
        ("competition_resolve", case_competition_resolve),
        ("competition_mismatch_repair", case_competition_mismatch_repair),
        ("misc_contract", case_misc_contract),
        ("audit_regression", case_audit_regression),
        ("checkpoint", case_checkpoint),
        ("checkpoint_resume", case_checkpoint_resume),
        ("coordinator_digest", case_coordinator_digest),
        ("bench_stats", case_bench_stats),
        ("agent_e2e", case_agent_e2e),
        ("agent_dry_run", case_agent_dry_run),
        ("agent_degraded", case_agent_degraded),
        ("agent_exception_safe", case_agent_exception_safe),
        ("agent_seed", case_agent_seed),
        ("agent_cli_entry", case_agent_cli_entry),
        ("agent_mcp_url", case_agent_mcp_url),
        ("crew_step_limit", case_crew_step_limit),
        ("jwt_forge", case_jwt_forge),
        ("http_p0", case_http_p0),
        ("http_burst", case_http_burst),
        ("ai_chat", case_ai_chat),
        ("ai_contract", case_ai_contract),
        ("ai_wiring", case_ai_wiring),
        ("ai_fixtures", case_ai_fixtures),
        ("ai_agent_e2e", case_ai_agent_e2e),
        ("http_302_cookie", case_http_302_cookie),
        ("context_compress", case_context_compress),
        ("misc_ext", case_misc_ext),
    ]
    for name, case in cases:
        try:
            case(tmp)
        except Exception:
            FAILED.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()

    print("-" * 48)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    from ctfy_agent.mcp_client import force_utf8_when_piped

    force_utf8_when_piped()
    sys.exit(main())
