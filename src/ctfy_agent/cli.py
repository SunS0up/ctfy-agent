#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口：doctor / tools / challenges / solve / state / demo。

设计原则：任何破坏性动作（提交 flag、启停实例）只在 solve 里由模型显式发起，
并且受 state 的幂等与上限保护；doctor 永远只做只读探测。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .budget import Budget
from .config import PROJECT_ROOT, Config, load
from .llm import DeepSeekLLM, LLMError
from .mcp_client import force_utf8_when_piped, mask_token
from .platform import CtfyPlatform
from .solver import Solver
from .state import Store, normalize_tags, now

EXIT = {"ok": 0, "config": 1, "network": 3, "llm": 4, "solve": 5}


# ---------- 公共 ----------


def build_config(args) -> Config:
    config = load(args.config)
    if getattr(args, "url", None):
        config.mcp_url = args.url
    if getattr(args, "model", None):
        config.model = args.model
    if getattr(args, "max_steps", None):
        config.max_steps = args.max_steps
    if getattr(args, "verbose", None):
        config.debug = True
    return config


def make_platform(config: Config, *, verbose: bool = False) -> CtfyPlatform:
    return CtfyPlatform(
        config.mcp_url,
        config.token,
        timeout=config.timeout,
        tool_map=config.extra.get("tool_map") or {},
        verbose=verbose,
    )


def make_llm(config: Config, model: str | None = None, budget=None) -> DeepSeekLLM:
    return DeepSeekLLM(
        api_key=config.llm_api_key,
        model=model or config.model,
        base_url=config.llm_base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        debug=config.debug,
        budget=budget,
    )


def normalize_challenge(item: dict) -> dict:
    """平台字段名未知，这里做一次宽松归一。"""
    def pick(*keys):
        for key in keys:
            if key in item and item[key] not in (None, ""):
                return item[key]
        return None

    return {
        "id": pick("id", "challenge_id", "slug", "code", "key", "name", "title"),
        "name": pick("name", "title", "challenge_name"),
        # 注意：tags 不再是 category 的兜底（那是审计查出的 bug：category 缺失时列表被当分类）
        "category": pick("category", "type", "kind"),
        "points": pick("points", "score", "value"),
        "description": pick("description", "content", "detail", "desc"),
        # 题型先验：平台免费给的解题线索，必须入库（曾因只映射 5 字段被整体丢弃）
        "tags": normalize_tags(pick("tags", "tag", "labels", "topics")),
        "difficulty": pick("difficulty", "level", "difficulty_level"),
        "solves_count": pick("solves_count", "solve_count", "solves", "solved", "solved_by"),
    }


def ensure_challenge_meta(platform, store: Store, challenge_id: str) -> str | None:
    """保证 state 里有这道题，且题型线索（difficulty/solves_count/tags）已入库。

    已在库但缺线索的题会补一次 get_challenge：老 state.json 里的题不会因为
    「库里已经有」就永远拿不到平台免费给的 tags —— 这正是上一轮 50 万 token
    盲跑的根因。返回 None 表示正常，返回字符串表示致命错误（调用方据此退出）。
    """
    item = store.get(challenge_id)
    if item is not None and (item.tags or item.difficulty):
        return None

    try:
        data = platform.get_challenge(challenge_id)
    except Exception as exc:  # noqa: BLE001 - 缺线索不该拦住解题
        if item is None:
            return f"取题目失败：{exc}"
        print(f"[warn] 题型线索补齐失败，按现有信息继续：{exc}", file=sys.stderr)
        return None

    normalized = normalize_challenge(data if isinstance(data, dict) else {"id": challenge_id})
    normalized["id"] = normalized.get("id") or challenge_id
    store.upsert(normalized)
    store.save()

    facts = store.get(challenge_id)
    if facts is not None and facts.facets():
        print(f"[题型] {challenge_id} {facts.facets()}")
    return None


def open_store(config: Config, run_id: str) -> Store:
    return Store(config.run_dir(run_id)).load()


# ---------- doctor ----------


def cmd_doctor(args) -> int:
    config = build_config(args)
    print("== 配置 ==")
    for key, value in config.masked().items():
        print(f"  {key:24s} {value}")

    problems = config.missing()
    if problems:
        print("\n[!] 还缺：")
        for item in problems:
            print(f"  - {item}")

    print("\n== MCP 握手 ==")
    if not config.token:
        print("  跳过：没有令牌")
        return EXIT["config"]

    platform = make_platform(config, verbose=config.debug)
    try:
        platform.connect()
    except Exception as exc:
        print(f"  FAIL {exc}")
        return EXIT["network"]

    info = platform.server_info
    print(f"  服务端   {info.get('name', '(未提供)')} {info.get('version', '')}".rstrip())
    print(f"  协议     {platform.client.protocol_version}")
    print(f"  会话     {platform.client.session_id or '(无状态模式)'}")
    print(f"  工具数   {len(platform.tools)}")

    print("\n== 语义动作映射 ==")
    for kind, name in platform.resolution_report().items():
        print(f"  {kind:18s} -> {name or '(未找到，需在 tool_map 里指定)'}")

    print("\n== DeepSeek V4.1 Flash ==")
    print(f"  模型     {config.model}")
    print(f"  端点     {config.llm_base_url}")
    print(f"  Key      {mask_token(config.llm_api_key)}")
    if not config.llm_api_key:
        print("  跳过探活：没有 DEEPSEEK_API_KEY")
    elif args.probe_llm:
        try:
            result = make_llm(config).ping()
            listed = ", ".join((result.get("models") or [])[:8]) or "(端点未返回模型列表)"
            print(f"  探活     OK，可用模型：{listed}")
        except LLMError as exc:
            print(f"  探活     FAIL {exc}")
    else:
        print("  探活     已跳过（加 --probe-llm 会真的打一次 /models）")

    print("\n== 费用刹车 ==")
    caps = Budget(
        max_tokens=config.max_tokens_per_challenge,
        max_calls=config.max_llm_calls,
        max_cost=config.max_cost,
        price_input=config.price_input_per_mtok,
        price_output=config.price_output_per_mtok,
    )
    print(f"  单题上限 {caps.describe()}")
    if config.max_cost > 0 and config.price_input_per_mtok <= 0 and config.price_output_per_mtok <= 0:
        print("  [!] 设了金额上限但没配单价（AGENT_PRICE_*_PER_MTOK），金额卡不住，只按 token 卡")
    elif config.max_cost <= 0:
        print("  提示     只按 token 卡；要卡金额需同时设 AGENT_MAX_COST 与 AGENT_PRICE_*_PER_MTOK")
    print("  说明     闸门在「每次调模型之前」，所以最多多花一次调用的量")

    print("\ndoctor 通过。下一步：python -m ctfy_agent tools")
    return EXIT["ok"]

# ---------- tools ----------


def cmd_tools(args) -> int:
    config = build_config(args)
    if not config.token:
        print("缺少 CTFY_TOKEN，先配置令牌。", file=sys.stderr)
        return EXIT["config"]

    platform = make_platform(config, verbose=config.debug)
    try:
        platform.connect()
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT["network"]

    print(f"工具 {len(platform.tools)} 个：")
    for tool in platform.tools:
        schema = tool.get("inputSchema") or {}
        required = schema.get("required") or []
        print(f"  - {tool.get('name')}  必填: {', '.join(required) or '无'}")
        description = (tool.get("description") or "").strip().replace("\n", " ")
        if description:
            print(f"      {description[:140]}")

    print("\n语义动作：")
    for kind, name in platform.resolution_report().items():
        print(f"  {kind:18s} -> {name or '(未找到)'}")

    target = PROJECT_ROOT / "docs" / "mcp-tools.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": now(),
        "endpoint": config.mcp_url,
        "protocol_version": platform.client.protocol_version,
        "server_info": platform.server_info,
        "resolution": platform.resolution_report(),
        "tools": platform.tools,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nschema 已写入 {target}")
    return EXIT["ok"]


# ---------- whoami / competitions ----------


def cmd_whoami(args) -> int:
    config = build_config(args)
    if not config.token:
        print("缺少 CTFY_TOKEN，先配置令牌。", file=sys.stderr)
        return EXIT["config"]
    platform = make_platform(config, verbose=config.debug)
    try:
        platform.connect()
        me = platform.whoami()
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT["network"]

    if not isinstance(me, dict):
        print(me)
        return EXIT["ok"]

    print(f"用户   {me.get('display_name')}  ({me.get('id')})")
    print(f"角色   {me.get('role')}   管理员={me.get('is_admin')}   令牌={me.get('token_kind')}")
    teams = me.get("competition_teams") or []
    if not teams:
        print("\n还没有加入任何比赛队伍。用 register_for_competition 报名（需先有队伍）。")
        return EXIT["ok"]

    print("\n参赛情况：")
    for team in teams:
        print(f"  - {team.get('competition_title')}")
        print(f"    队伍={team.get('team_name')}  角色={team.get('role')}  阶段={team.get('competition_phase')}")
        print(f"    比赛实例={team.get('running_instances')}  排名={team.get('my_rank')}  队伍数={team.get('competition_team_count')}")
        print(f"    competition_id={team.get('competition_id')}")
        print(f"    赛期 {team.get('starts_at')} → {team.get('ends_at')}")
    return EXIT["ok"]


def cmd_competitions(args) -> int:
    config = build_config(args)
    platform = make_platform(config, verbose=config.debug)
    try:
        platform.connect()
        rows = platform.list_competitions(phase=args.phase or None)
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT["network"]

    if not isinstance(rows, list):
        print(rows)
        return EXIT["ok"]

    print(f"{len(rows)} 场比赛")
    print(f"{'competition_id':<34}{'阶段':<12}{'开始':<22}标题")
    for row in rows:
        if not isinstance(row, dict):
            continue
        print(
            f"{str(row.get('id'))[:33]:<34}{str(row.get('status') or '-'):<12}"
            f"{str(row.get('starts_at') or '-')[:21]:<22}{str(row.get('title') or '')[:40]}"
        )
    return EXIT["ok"]


# ---------- challenges ----------


def cmd_challenges(args) -> int:
    config = build_config(args)
    platform = make_platform(config, verbose=config.debug)
    try:
        platform.connect()
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT["network"]

    competition_id = args.competition
    if not competition_id:
        # 自动从「我加入的队伍」里挑：优先进行中/即将开始，且标题含 TPCTF
        try:
            me = platform.whoami()
        except Exception:
            me = None
        teams = (me or {}).get("competition_teams") if isinstance(me, dict) else None
        candidates = [t for t in (teams or []) if isinstance(t, dict)]
        candidates.sort(key=lambda t: (0 if "TPCTF" in str(t.get("competition_title")) else 1,
                                       0 if t.get("competition_phase") in ("running", "upcoming") else 1))
        if candidates:
            competition_id = candidates[0].get("competition_id")
            print(f"（自动选用比赛：{candidates[0].get('competition_title')}）")

    if not competition_id:
        print("没有可用的比赛。先跑 `run.cmd competitions` 看列表，再用 --competition <id> 指定。", file=sys.stderr)
        return EXIT["config"]

    try:
        raw = platform.list_challenges(competition_id)
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT["network"]

    if isinstance(raw, str):
        print(raw)
        return EXIT["ok"]

    store = open_store(config, args.run_id)
    store.meta["competition_id"] = competition_id
    rows = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        normalized = normalize_challenge(item)
        challenge_id = normalized.get("id")
        if not challenge_id:
            continue
        # 审计 L7：归一化 id 碰撞时跳过并告警，避免两道题互相覆盖
        if str(challenge_id) in seen:
            print(f"[warn] 题目 id 冲突，已跳过重复项：{challenge_id!r}", file=sys.stderr)
            continue
        seen.add(str(challenge_id))
        store.upsert(normalized)
        rows.append(normalized)

    print(f"{len(rows)} 道题（已写入 {store.path}，competition_id={competition_id}）")
    print(f"{'ID':<24}{'分类':<10}{'分值':<8}名称")
    for row in rows:
        print(f"{str(row['id'])[:23]:<24}{str(row['category'] or '-')[:9]:<10}"
              f"{str(row['points'] or '-')[:7]:<8}{str(row['name'] or '')[:40]}")
    store.save()
    return EXIT["ok"]


# ---------- solve ----------


def cmd_solve(args) -> int:
    config = build_config(args)
    if config.missing():
        print("配置不完整：" + "；".join(config.missing()), file=sys.stderr)
        return EXIT["config"]

    platform = make_platform(config, verbose=config.debug)
    try:
        platform.connect()
    except Exception as exc:
        print(f"MCP 连接失败：{exc}", file=sys.stderr)
        return EXIT["network"]

    store = open_store(config, args.run_id)
    problem = ensure_challenge_meta(platform, store, args.challenge_id)
    if problem:
        print(problem, file=sys.stderr)
        return EXIT["solve"]

    def on_event(kind, payload=None):
        if kind == "step":
            content = (payload or {}).get("content") or ""
            calls = (payload or {}).get("tool_calls") or []
            names = [((call.get("function") or {}).get("name")) for call in calls] or ["(无工具调用)"]
            print(f"[step {payload['step']}] {content[:120]}")
            print(f"           -> {', '.join(filter(None, names))}")
        elif kind == "tool":
            print(f"           [tool] {payload['name']}({json.dumps(payload['args'], ensure_ascii=False)[:120]})")
        elif kind == "flag_accepted":
            print(f"           [!!] 平台确认通过：{payload['challenge_id']}")
        elif kind == "instance_started":
            print(f"           [实例] {json.dumps(payload['instance'], ensure_ascii=False)[:160]}")
        elif kind == "instance_start_failed":
            print(f"           [实例] 自动启动失败，交给模型处理：{payload['error'][:160]}")

    solver = Solver(config, platform, make_llm(config), store, on_event=on_event)
    try:
        summary = solver.solve(args.challenge_id, max_steps=args.max_steps)
    except LLMError as exc:
        print(f"模型调用失败：{exc}", file=sys.stderr)
        return EXIT["llm"]

    print("\n== 结果 ==")
    for key, value in summary.items():
        print(f"  {key:12s} {value}")
    return EXIT["ok"] if summary.get("submitted") else EXIT["solve"]


# ---------- crew（多智能体协作） ----------


def cmd_crew(args) -> int:
    from .crew import Crew  # noqa: PLC0415 - 只有 crew 命令需要

    config = build_config(args)
    if config.missing():
        print("配置不完整：" + "；".join(config.missing()), file=sys.stderr)
        return EXIT["config"]

    platform = make_platform(config, verbose=config.debug)
    try:
        platform.connect()
    except Exception as exc:
        print(f"MCP 连接失败：{exc}", file=sys.stderr)
        return EXIT["network"]

    store = open_store(config, args.run_id)
    problem = ensure_challenge_meta(platform, store, args.challenge_id)
    if problem:
        print(problem, file=sys.stderr)
        return EXIT["solve"]

    def on_event(kind, payload=None):
        if kind == "delegate":
            print(f"[协调] 派活 -> {payload['role']}：{payload['task'][:120]}")
        elif kind == "report":
            print(f"[协调] {payload['role']} 交结论（{payload['report'][:160]}）")
        elif kind == "flag_accepted":
            print(f"           [!!] 平台确认通过：{payload['challenge_id']}")
        elif kind == "instance_started":
            print(f"           [实例] {json.dumps(payload['instance'], ensure_ascii=False)[:160]}")
        elif kind == "instance_start_failed":
            print(f"           [实例] 自动启动失败，交给协调者处理：{payload['error'][:160]}")

    crew = Crew(config, platform, store, lambda model=None: make_llm(config, model=model), on_event=on_event)
    try:
        summary = crew.solve(args.challenge_id)
    except LLMError as exc:
        print(f"模型调用失败：{exc}", file=sys.stderr)
        return EXIT["llm"]

    print("\n== 结果 ==")
    for key, value in summary.items():
        print(f"  {key:12s} {value}")
    return EXIT["ok"] if summary.get("submitted") else EXIT["solve"]


# ---------- auto（无人值守批量） ----------


def cmd_auto(args) -> int:
    from .auto import run_auto  # noqa: PLC0415
    from .crew import Crew  # noqa: PLC0415

    config = build_config(args)
    if config.missing():
        print("配置不完整：" + "；".join(config.missing()), file=sys.stderr)
        return EXIT["config"]

    platform = make_platform(config, verbose=config.debug)
    try:
        platform.connect()
    except Exception as exc:
        print(f"MCP 连接失败：{exc}", file=sys.stderr)
        return EXIT["network"]

    store = open_store(config, args.run_id)
    if not store.challenges:
        print("本地没有题单，先跑一次 `challenges` 同步。", file=sys.stderr)
        return EXIT["config"]

    per_challenge = config.max_tokens_per_challenge
    total_cap = args.max_total_tokens

    def on_event(kind, payload=None):
        if kind == "plan":
            print(f"[auto] 计划 {len(payload['candidates'])} 道：{', '.join(payload['candidates'][:8])}")
        elif kind == "done":
            flag = "✓" if payload["submitted"] else "✗"
            print(f"[auto] {flag} {payload['challenge_id']}  {payload['tokens']} token  累计 {payload['spent']}")
        elif kind == "failed":
            print(f"[auto] ! {payload['challenge_id']}  {payload['error'][:120]}")
        elif kind == "stop":
            print(f"[auto] 停止：{payload['reason']}")

    crew = Crew(config, platform, store, lambda model=None: make_llm(config, model=model), on_event=None)

    def solve_one(challenge_id: str) -> dict:
        # 剩余额度动态收紧单题上限，避免最后一道题把总预算吃穿
        if total_cap:
            remaining = total_cap - spent["tokens"]
            if remaining > 0:
                config.max_tokens_per_challenge = min(per_challenge, remaining)
        return crew.solve(challenge_id)

    spent = {"tokens": 0}
    original_solve = solve_one

    def solve_tracked(challenge_id: str) -> dict:
        summary = original_solve(challenge_id)
        # 与 run_auto 一致：只累计本轮 delta（审计 B9），否则重试会重复计入历史量
        spent["tokens"] += int(summary.get("tokens_spent") or (summary.get("budget") or {}).get("total_tokens") or 0)
        return summary

    summary = run_auto(
        store=store,
        solve=solve_tracked,
        statuses=tuple(args.status),
        limit=args.limit,
        max_total_tokens=total_cap,
        max_failures=args.max_failures,
        on_event=on_event,
    )

    print("\n== 批量结果 ==")
    for key in ("planned", "attempted", "submitted", "failed", "spent", "stopped"):
        print(f"  {key:10s} {summary[key]}")
    return EXIT["ok"] if summary["submitted"] else EXIT["solve"]


# ---------- state ----------


def cmd_state(args) -> int:
    config = build_config(args)
    store = open_store(config, args.run_id)
    if not store.challenges:
        print(f"{store.path} 里还没有题目，先跑 challenges --sync")
        return EXIT["ok"]

    print(f"{'ID':<22}{'状态':<15}{'提交':<6}{'步骤':<6}{'token':<9}{'费用':<8}名称")
    for item in store.challenges.values():
        tokens = item.tokens or {}
        used = int(tokens.get("total_tokens") or 0)
        spent = float(tokens.get("cost") or 0.0)
        print(
            f"{item.id[:21]:<22}{item.status:<15}{item.attempts:<6}{item.steps:<6}"
            f"{used:<9}{f'{spent:.4f}':<8}{item.name[:30]}"
        )
    return EXIT["ok"]


# ---------- demo ----------


def cmd_demo(args) -> int:
    """离线演练：假 MCP + 脚本化 LLM，验证循环、幂等与落盘。不需要令牌和 Key。"""
    from .testing import fake_llm  # noqa: PLC0415 - 只在 demo 里需要

    server, url = fake_llm.start_fake_mcp(mode=args.mode)
    print(f"[demo] 假 MCP 已启动 {url}（{args.mode} 模式）")
    try:
        config = load()
        config.mcp_url = url
        config.token = "pf_demo_token"
        config.model = "deepseek-v4.1-flash"
        config.runs_dir = str((PROJECT_ROOT / "runs" / "demo").resolve())
        config.max_steps = 6

        platform = CtfyPlatform(url, config.token)
        platform.connect()
        store = Store(PROJECT_ROOT / "runs" / "demo").load()
        store.challenges.clear()

        challenges = platform.list_challenges()
        print(f"[demo] 平台返回 {len(challenges)} 道题")
        for item in challenges[:1]:
            store.upsert(normalize_challenge(item))
        store.save()

        challenge_id = next(iter(store.challenges))
        llm = fake_llm.ScriptedLLM(fake_llm.demo_script())

        solver = Solver(
            config,
            platform,
            llm,
            store,
            on_event=lambda kind, payload=None: print(f"[demo] {kind}: {json.dumps(payload, ensure_ascii=False)[:220]}"),
        )
        summary = solver.solve(challenge_id)
        print("\n[demo] 结果：")
        for key, value in summary.items():
            print(f"  {key:12s} {value}")
        print(f"\n[demo] 转录：{summary['transcript']}")
        print(f"[demo] 状态：{store.path}")
        return EXIT["ok"] if summary.get("submitted") else EXIT["solve"]
    finally:
        server.shutdown()
        server.server_close()


# ---------- main ----------


def cmd_agent(args) -> int:
    from .agent_main import main as agent_main  # noqa: PLC0415 - 容器适配层单独成模块

    argv = []
    if getattr(args, "task", None):
        argv += ["--task", args.task]
    return agent_main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctfy_agent",
        description="TPCTF 2026 智能体赛道 · 解题框架（DeepSeek V4.1 Flash）",
    )
    parser.add_argument("--config", help="配置文件路径，默认 config/agent.json")
    parser.add_argument("--url", help="覆盖 MCP 端点")
    parser.add_argument("--model", help="覆盖模型名，默认 deepseek-v4.1-flash")
    parser.add_argument("--max-steps", type=int, help="覆盖每题步数预算")
    parser.add_argument("--run-id", default="default", help="状态目录名，默认 default")
    parser.add_argument("--verbose", action="store_true", help="打印协议细节")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doctor = sub.add_parser("doctor", help="检查令牌、MCP 握手、模型配置")
    p_doctor.add_argument("--probe-llm", action="store_true", help="真的调用一次 /models 探活")

    sub.add_parser("tools", help="列出平台工具并写入 docs/mcp-tools.json")
    sub.add_parser("whoami", help="看令牌身份、队伍与参赛状态")

    p_comps = sub.add_parser("competitions", help="列出比赛")
    p_comps.add_argument("--phase", choices=["upcoming", "running", "past"], default=None)

    p_chal = sub.add_parser("challenges", help="拉取题单并同步到 state")
    p_chal.add_argument("--competition", help="比赛 ID；不填则自动选用你已报名的比赛")

    sub.add_parser("state", help="查看本地 state")

    p_solve = sub.add_parser("solve", help="对一道题跑解题循环")
    p_solve.add_argument("challenge_id")
    p_solve.add_argument("--max-steps", type=int, help="覆盖步数预算")

    p_crew = sub.add_parser("crew", help="多智能体协作解题（协调者 + web/rev/crypto/misc 专家）")
    p_crew.add_argument("challenge_id")
    p_crew.add_argument("--max-steps", type=int, help="覆盖协调者回合数")

    p_auto = sub.add_parser("auto", help="无人值守批量解本地题单（单题失败不中断）")
    p_auto.add_argument("--status", nargs="*", default=["new", "parsed", "instance_up", "stalled"],
                        help="只跑这些状态的题")
    p_auto.add_argument("--limit", type=int, default=0, help="最多跑几道（0=不限）")
    p_auto.add_argument("--max-total-tokens", type=int, default=0, help="整批 token 上限（0=不限）")
    p_auto.add_argument("--max-failures", type=int, default=3, help="连续失败几次就停")

    p_demo = sub.add_parser("demo", help="离线演练（假 MCP + 脚本化 LLM）")
    p_demo.add_argument("--mode", choices=["json", "sse"], default="json")

    p_agent = sub.add_parser("agent", help="容器适配层：读 task.json → 跑 Crew → 写 result.json（CTFY-0001 v2）")
    p_agent.add_argument("--task", help="task.json 路径（默认 $CTFY_WORK_DIR/task.json）")

    return parser


def main(argv=None) -> int:
    force_utf8_when_piped()
    args = build_parser().parse_args(argv)
    handlers = {
        "doctor": cmd_doctor,
        "tools": cmd_tools,
        "whoami": cmd_whoami,
        "competitions": cmd_competitions,
        "challenges": cmd_challenges,
        "solve": cmd_solve,
        "crew": cmd_crew,
        "auto": cmd_auto,
        "state": cmd_state,
        "demo": cmd_demo,
        "agent": cmd_agent,
    }
    try:
        return handlers[args.cmd](args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # 顶层兜底：给出可读错误而不是整段 traceback
        if os.environ.get("AGENT_DEBUG", "").lower() in ("1", "true", "yes"):
            raise
        print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT["config"]
