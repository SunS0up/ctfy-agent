#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器适配层：`$CTFY_WORK_DIR/task.json` → Config → Crew → `result.json`（纯 stdlib）。

实现 CTFY-0001 v2 的用户镜像契约，最小合规集只有三条，本文件把它们当成不变量：
  1. `/ctfy/work/result.json` 必须存在（任何路径都写，包括被 kill / 超时 / 顶层异常）；
  2. 必须 `exit 0`（非零退出码 = 判 failed，见规范 §3「A non-zero exit is recorded as a
     failed run regardless of what you wrote」）；
  3. 日志一律走 stderr，stdout 最多留一行最终 rollup JSON（用户镜像的 stdout 本就不被读，
     只当本地调试用）。

关键决策（与草稿 b1-adapter-plan 的差别见 docs/b1-adapter-plan.md 附注）：
  - 容器内**永不自己 stop_instance**：平台注入 `CTFY_EVAL_KEEP_INSTANCE=1`，dispatcher 回收。
  - `solved` 是**题目 id 列表**，`answers` 才是平台代交通道；两者都只写已校验的答案。
  - flavor 未知时 fail loudly：非 `openai` 一律 `refused`，不烧钱。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
from pathlib import Path

from .config import Config, load
from .crew import Crew
from .llm import DeepSeekLLM
from .platform import CtfyPlatform
from .state import Store

SCHEMA_VERSION = 2
DEFAULT_WORK_DIR = "/ctfy/work"
DEFAULT_MAX_TURNS = 80          # task.json.max_turns 为 null（无预算）时的保守上限
DEFAULT_MAX_TOKENS = 8192       # 4096 会在 thinking 块截断、一步即「无动作结束」
DEFAULT_MAX_CONTEXT = 16000
DEFAULT_MAX_TOKENS_PER_CHALLENGE = 800000

_RESULT_PATH: Path | None = None
_CHALLENGE_ID = ""


# ---------- 基础工具 ----------


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def _load_json(path: Path) -> dict | None:
    """宽容读：缺文件 / 坏 JSON 都返回 None，调用方降级处理。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(text: str, env) -> str:
    """展开 `${CTFY_TOKEN}` 这类占位——平台在 mcp.json 里写的是字面量，不是值。"""
    def repl(match):
        return env.get(match.group(1), "")

    return _ENV_RE.sub(repl, text)


def _as_int(value, default):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------- rollup ----------


def _rollup(*, challenge_id="", instance_id="", answers=None, solved=None,
            fully_solved=False, turns=0, tool_calls_made=0, input_tokens=0,
            output_tokens=0, stop_reason="done", writeup="") -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "challenge_id": challenge_id,
        "instance_id": instance_id,
        "answers": answers or {},
        "solved": solved or [],
        "fully_solved": fully_solved,
        "turns": turns,
        "tool_calls_made": tool_calls_made,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "stop_reason": stop_reason,
        "writeup": writeup,
    }


def _write_rollup(path: Path, rollup: dict, *, announce: bool = False) -> None:
    """原子写 result.json（tmp + os.replace → 一定是普通文件，O_NOFOLLOW 拒读不到符号链接）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(rollup, ensure_ascii=False)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    if announce:
        print(text)  # stdout 一行最终回执（仅本地调试用）


_STOP_MAP = {
    "flag_accepted": "done",
    "coordinator_done": "done",
    "no_more_actions": "done",
    "step_limit": "max_turns",
    "budget_exhausted": "cut_off",
    "llm_error": "halted",
}


def _map_stop_reason(reason: str) -> str:
    return _STOP_MAP.get(reason, "done")


def _count_tool_calls(transcript_path) -> int:
    try:
        total = 0
        for line in Path(transcript_path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            total += len(rec.get("tool_calls") or [])
        return total
    except (OSError, ValueError, TypeError):
        return 0


# ---------- 信号：被 kill / 超时也有文件 ----------


def _on_signal(signum, frame):
    if _RESULT_PATH is not None:
        _write_rollup(
            _RESULT_PATH,
            _rollup(challenge_id=_CHALLENGE_ID, stop_reason="halted",
                    writeup=f"signal {signum}"),
        )
    os._exit(0)


def _register_signals() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass  # 非主线程里注册会失败，忽略


# ---------- MCP 端点解析 ----------


def _resolve_mcp_url(work_dir: Path, platform_url: str, env) -> str | None:
    """MCP 端点两条路：mcp.json（平台写好的客户端配置）→ CTFY_PLATFORM_URL + /api/v1/mcp/。

    BYO 镜像拿不到 CTFY_MCP_URL；mcp.json 里的 url 带 ${VAR} 字面量，要自己展开。
    """
    mcp_json = work_dir / "mcp.json"
    if mcp_json.is_file():
        data = _load_json(mcp_json)
        servers = (data or {}).get("mcpServers") or {}
        for srv in servers.values():
            url = (srv or {}).get("url")
            if url:
                return _expand_env(str(url), env)
    if platform_url:
        return platform_url.rstrip("/") + "/api/v1/mcp/"
    return None


# ---------- Config 组装 ----------


def _build_config(task: dict | None, work_dir: Path, mcp_url: str | None, env) -> Config:
    # 从 config/agent.json 拿角色 profile（web allow_shell 等），再逐项覆盖容器关键字段
    config = load(use_env_file=False)

    max_turns = None
    if task:
        max_turns = _as_int(task.get("max_turns"), None)
    if max_turns is None:
        max_turns = _as_int(env.get("CTFY_EVAL_MAX_TURNS"), None)

    questions = (task or {}).get("questions") or []
    gated = _as_int((task or {}).get("gated_questions"), 0)

    config.mcp_url = mcp_url or config.mcp_url
    config.token = str(env.get("CTFY_TOKEN") or "")
    config.llm_api_key = str(env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY") or "")
    config.llm_base_url = str(env.get("LLM_BASE_URL") or env.get("OPENAI_BASE_URL") or config.llm_base_url)
    config.model = str(env.get("LLM_MODEL") or env.get("OPENAI_MODEL") or config.model)
    config.max_tokens = _as_int(env.get("AGENT_MAX_TOKENS"), DEFAULT_MAX_TOKENS)
    config.max_llm_calls = max_turns or _as_int(env.get("AGENT_MAX_TURNS"), DEFAULT_MAX_TURNS)
    config.max_context_tokens = _as_int(env.get("AGENT_MAX_CONTEXT_TOKENS"), DEFAULT_MAX_CONTEXT)
    config.max_tokens_per_challenge = _as_int(
        env.get("AGENT_MAX_TOKENS_PER_CHALLENGE"), DEFAULT_MAX_TOKENS_PER_CHALLENGE
    )
    config.submit_limit = max(3, (len(questions) + gated) * 2)
    config.runs_dir = str(work_dir / "runs")

    # 归一化角色 profile：网关会按 LLM_MODEL 改写 model（多模型名是误导），
    # 且 agent.json 里 web 的 max_tokens=4096 会截断思考块——统一到容器默认值。
    for spec in (config.extra.get("roles") or {}).values():
        if isinstance(spec, dict):
            spec.pop("model", None)
            spec["max_tokens"] = config.max_tokens

    # 容器内实例生命周期归平台：永不自己 stop_instance
    config.extra["keep_instance_on_success"] = True
    config.extra["keep_instance_on_fail"] = True

    competition_id = (task or {}).get("competition_id") or env.get("CTFY_EVAL_COMPETITION_ID", "")
    if competition_id:
        config.extra["competition_id"] = str(competition_id)
    if questions:
        config.extra["default_question_id"] = str(questions[0].get("id") or "flag")
    return config


# ---------- 题面入库 ----------


def _seed_challenge(store: Store, task: dict | None, env) -> str:
    """把 task.json 的题面写进 state。只信 task.json（权威），题型线索交给模型自己 get_challenge。"""
    task = task or {}
    challenge = task.get("challenge") or {}
    cid = str(challenge.get("id") or env.get("CTFY_EVAL_CHALLENGE_ID") or "challenge")

    desc = str(challenge.get("description") or "")
    blocks = []
    for q in task.get("questions") or []:
        qid = str(q.get("id") or "flag")
        prompt = str(q.get("prompt") or "")
        line = f"【问题 {qid}】{prompt}" if prompt else f"【问题 {qid}】"
        if qid != "flag":
            line += f"（submit 时 question_id 用 `{qid}`）"
        blocks.append(line)
    gated = _as_int(task.get("gated_questions"), 0)
    if gated and gated > 0:
        blocks.append(
            f"【门控题】本题还有 {gated} 道门控题未解锁：每答对一题后必须重新 "
            "get_challenge 拉取新题面，否则会漏掉后续分。"
        )
    if blocks:
        desc = (desc + "\n\n" if desc else "") + "\n".join(blocks)

    store.upsert(
        {
            "id": cid,
            "name": str(challenge.get("name") or ""),
            "description": desc,
        }
    )
    store.save()
    return cid


def _enrich_facets(store: Store, platform, cid: str) -> None:
    """best-effort 题型线索入库（category/tags/difficulty/solves_count），失败不拦路。

    只补「题型先验」四样，绝不碰 name/description——seed 里拼进去的【问题】与门控题提示
    不能被 get_challenge 的原文覆盖。A3 方法卡 / brief 题型行 / 初始提示都读这里的 tags。
    解题循环内 get_challenge 只回文本、不回写 store，所以这一刀必须在开跑前做。
    """
    item = store.get(cid)
    if item is not None and (item.tags or item.difficulty):
        return  # 已有题型线索，不重复取
    try:
        data = platform.get_challenge(cid)
    except Exception as exc:  # noqa: BLE001 - 线索缺失不该拦住解题
        _log(f"[warn] 题型线索补齐失败（继续解题）：{exc}")
        return
    if not isinstance(data, dict):
        return
    from .cli import normalize_challenge  # noqa: PLC0415 - 复用平台字段名归一

    norm = normalize_challenge(data)
    store.upsert(
        {
            "id": cid,
            "category": norm.get("category"),
            "tags": norm.get("tags"),
            "difficulty": norm.get("difficulty"),
            "solves_count": norm.get("solves_count"),
        }
    )
    store.save()


# ---------- 最终 rollup ----------


def _final_rollup(store: Store, challenge_id: str, summary: dict, config: Config) -> dict:
    challenge = store.get(challenge_id)
    qid = str(config.extra.get("default_question_id") or "flag")

    answers: dict = {}
    solved: list = []
    if challenge is not None and challenge.flag_value:
        answers[qid] = challenge.flag_value  # flag_value 只在平台校验通过后记录
    if summary.get("submitted"):
        solved = [qid]

    usage = summary.get("usage") or {}
    budget = summary.get("budget") or {}
    ledger = summary.get("ledger") or {}
    reason = str(summary.get("reason") or "done")
    writeup = (
        f"reason={reason} steps={summary.get('steps')} "
        f"tokens={summary.get('tokens_spent', 0)} "
        f"ledger={json.dumps(ledger, ensure_ascii=False)}"
    )

    return _rollup(
        challenge_id=challenge_id,
        instance_id=(challenge.instance_id if challenge is not None else ""),
        answers=answers,
        solved=solved,
        fully_solved=bool(summary.get("submitted")),
        turns=int(budget.get("calls") or summary.get("steps") or 0),
        tool_calls_made=_count_tool_calls(summary.get("transcript")),
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        stop_reason=_map_stop_reason(reason),
        writeup=writeup[:2000],
    )


# ---------- 主流程 ----------


def run_agent(
    *,
    task_path: Path,
    work_dir: Path,
    env=None,
    platform: CtfyPlatform | None = None,
    llm_factory=None,
    result_path: Path | None = None,
) -> int:
    """跑一整次 agent。返回退出码（按契约恒为 0；失败语义全部写进 stop_reason）。

    `platform` / `llm_factory` / `result_path` 是测试注入点：给了就用注入的，
    不给就从 env 构建真实客户端、结果路径取 task.json 的 result.path。
    """
    env = dict(env if env is not None else os.environ)
    global _RESULT_PATH, _CHALLENGE_ID  # noqa: PLW0603 - 信号处理器需要

    task = _load_json(Path(task_path))
    challenge_id = str((task or {}).get("challenge", {}).get("id") or env.get("CTFY_EVAL_CHALLENGE_ID", ""))
    _CHALLENGE_ID = challenge_id
    if result_path is None:
        result_path = work_dir / "result.json"
        if task:
            result_path = Path(task.get("result", {}).get("path") or result_path)
    else:
        result_path = Path(result_path)
    _RESULT_PATH = result_path

    # 0) 先写 provisional：之后任何一步挂了 / 被 kill，盘上都有文件
    _write_rollup(
        result_path,
        _rollup(challenge_id=challenge_id, stop_reason="halted", writeup="provisional"),
    )

    # 1) dry run：注册门，最便宜，且不碰网络、不要凭证
    if env.get("CTFY_EVAL_DRY_RUN") == "1" or (task or {}).get("dry_run"):
        _write_rollup(
            result_path,
            _rollup(challenge_id=challenge_id, stop_reason="dry_run"),
            announce=True,
        )
        return 0

    # 2) task.json 必须存在且可解析
    if task is None:
        _write_rollup(
            result_path,
            _rollup(challenge_id=challenge_id, stop_reason="halted",
                    writeup="task.json missing or malformed"),
            announce=True,
        )
        return 0

    # 3) 只支持 instance 模式
    run_mode = str(task.get("run_mode") or "")
    if run_mode != "instance":
        _write_rollup(
            result_path,
            _rollup(challenge_id=challenge_id, stop_reason="refused",
                    writeup=f"unsupported run_mode={run_mode!r}"),
            announce=True,
        )
        return 0

    # 4) 凭证 + flavor 校验（fail loudly，不烧钱）
    flavor = str(env.get("LLM_FLAVOR") or "")
    token = str(env.get("CTFY_TOKEN") or "")
    api_key = str(env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY") or "")
    if flavor != "openai":
        _write_rollup(
            result_path,
            _rollup(challenge_id=challenge_id, stop_reason="refused",
                    writeup=f"unsupported LLM_FLAVOR={flavor!r} (only openai client is bundled)"),
            announce=True,
        )
        return 0
    if not token or not api_key:
        _write_rollup(
            result_path,
            _rollup(challenge_id=challenge_id, stop_reason="refused",
                    writeup="missing CTFY_TOKEN or LLM_API_KEY"),
            announce=True,
        )
        return 0

    # 5) MCP 端点
    mcp_url = None
    if platform is None:
        mcp_url = _resolve_mcp_url(work_dir, str(env.get("CTFY_PLATFORM_URL") or ""), env)
        if not mcp_url:
            _write_rollup(
                result_path,
                _rollup(challenge_id=challenge_id, stop_reason="halted",
                        writeup="no MCP endpoint (no mcp.json, no CTFY_PLATFORM_URL)"),
                announce=True,
            )
            return 0

    config = _build_config(task, work_dir, mcp_url, env)

    # 6) 平台连接（或复用注入）
    if platform is None:
        platform = CtfyPlatform(mcp_url, token, timeout=config.timeout)
        try:
            platform.connect()
        except Exception as exc:  # noqa: BLE001
            _write_rollup(
                result_path,
                _rollup(challenge_id=challenge_id, stop_reason="halted",
                        writeup=f"MCP connect failed: {exc}"),
                announce=True,
            )
            return 0

    # 7) state + 题面入库
    try:
        run_id = challenge_id or "run"
        store = Store(config.run_dir(run_id)).load()
    except ValueError as exc:
        _write_rollup(
            result_path,
            _rollup(challenge_id=challenge_id, stop_reason="halted", writeup=f"bad run id: {exc}"),
            announce=True,
        )
        return 0
    challenge_id = _seed_challenge(store, task, env)  # 回写：兜底 id 与 store/solve/rollup 一致
    _CHALLENGE_ID = challenge_id
    _enrich_facets(store, platform, challenge_id)

    # 8) LLM 工厂（或复用注入）；强制所有角色用 LLM_MODEL（网关会改 model，多模型名是误导）
    if llm_factory is None:
        def llm_factory(model=None):
            return DeepSeekLLM(
                api_key=config.llm_api_key,
                model=config.model,
                base_url=config.llm_base_url,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                debug=config.debug,
            )

    # 9) 跑 Crew
    try:
        crew = Crew(config, platform, store, llm_factory)
        summary = crew.solve(challenge_id)
    except Exception as exc:  # noqa: BLE001 - 任何解题异常都不许毁掉 result.json
        _write_rollup(
            result_path,
            _rollup(challenge_id=challenge_id, stop_reason="halted", writeup=f"crew failed: {exc}"),
            announce=True,
        )
        return 0

    # 10) 最终 rollup
    _write_rollup(result_path, _final_rollup(store, challenge_id, summary, config), announce=True)
    _RESULT_PATH = None  # 结果已落盘：随后 SIGTERM 变纯 exit 0，不再用空 answers 覆盖
    return 0


def main(argv=None) -> int:
    global _RESULT_PATH, _CHALLENGE_ID  # noqa: PLW0603 - 信号处理器需要

    parser = argparse.ArgumentParser(
        prog="ctfy_agent agent",
        description="容器适配层：读 task.json → 跑 Crew → 写 result.json（CTFY-0001 v2）。",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="task.json 路径（默认 $CTFY_WORK_DIR/task.json，本地测试用绝对路径覆盖）",
    )
    args = parser.parse_args(argv)

    work_dir = Path(os.environ.get("CTFY_WORK_DIR") or DEFAULT_WORK_DIR)
    task_path = Path(args.task) if args.task else work_dir / "task.json"
    if args.task and not os.environ.get("CTFY_WORK_DIR"):
        # 本地测试：--task 给定后，把结果写到 fixture 同目录，避免碰 /ctfy/work
        work_dir = task_path.resolve().parent

    # 信号只在真实入口注册（run_agent 保持纯函数，测试注入点无全局副作用）
    _RESULT_PATH = None
    _CHALLENGE_ID = ""
    _register_signals()
    try:
        return run_agent(task_path=task_path, work_dir=work_dir)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 顶层兜底：绝不带非零码退出
        _log(f"[fatal] {type(exc).__name__}: {exc}")
        if _RESULT_PATH is not None:
            try:
                _write_rollup(
                    _RESULT_PATH,
                    _rollup(challenge_id=_CHALLENGE_ID, stop_reason="halted",
                            writeup=f"{type(exc).__name__}: {exc}"),
                )
            except Exception:  # noqa: BLE001 - 写盘也失败就放弃，仍保证 exit 0
                _log("[fatal] 兜底写 result.json 也失败")
        return 0


if __name__ == "__main__":
    sys.exit(main())
