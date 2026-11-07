"""本地跑一遍 engagement 模式：假平台 + 真模型，验证端到端能出报告。

用途：不烧平台的一次真实测评，就在本机确认
  1. run_mode=engagement 不再被 refused；
  2. 报告真的由模型写出来、并调用 save_report / finalize_report；
  3. result.json 的字段（stop_reason / writeup / solved / fully_solved）符合契约。

做法：给 run_agent 注入假 platform（只认 save_report / finalize_report）和真 DeepSeekLLM。
账目仍走真网关，所以 --turns 就是钱：默认 10 轮。

用法：
  set LLM_API_KEY=sk-...
  python tools/engagement_local.py --turns 10
  python tools/engagement_local.py --task tests/fixtures/task_engagement.json --model deepseek-flash
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ctfy_agent.agent_main import run_agent  # noqa: E402
from ctfy_agent.llm import DeepSeekLLM  # noqa: E402


class FakePlatform:
    """只记录报告相关调用；其余工具调用一律拒绝（本地不碰 MCP）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.report = ""

    def call_raw(self, name: str, args: dict | None = None) -> dict:
        args = dict(args or {})
        self.calls.append((name, args))
        if name == "save_report":
            self.report = str(args.get("content") or "")
            return {"ok": True, "version": 1, "chars": len(self.report)}
        if name == "finalize_report":
            return {"ok": True, "finalized": True, "grade": {"pending": True}}
        raise RuntimeError(f"fake platform: {name} 未实现（本地只验证报告链路）")

    def call_text(self, name: str, args: dict | None = None) -> str:
        return json.dumps(self.call_raw(name, args), ensure_ascii=False)

    def close(self) -> None:  # 与 CtfyPlatform 对齐，run_agent 结束时可能调用
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本地 engagement 端到端验证（假平台 + 真模型）")
    parser.add_argument("--task", default=str(ROOT / "tests" / "fixtures" / "task_engagement.json"))
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL") or "deepseek-flash")
    parser.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1")
    parser.add_argument("--turns", type=int, default=10, help="模型调用上限（= 花费上限）")
    parser.add_argument("--work-dir", default=str(ROOT / ".local-run"))
    args = parser.parse_args(argv)

    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    if not api_key:
        print("缺少 LLM_API_KEY（或 DEEPSEEK_API_KEY），先 set 再跑", file=sys.stderr)
        return 2

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    task_path = Path(args.task)
    result_path = work_dir / "result.json"
    # fixture 里的 result.path 是容器内路径，本地跑用 --work-dir 下的
    task = json.loads(task_path.read_text(encoding="utf-8"))
    local_task = work_dir / "task.json"
    task.setdefault("result", {})["path"] = str(result_path)
    local_task.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")

    platform = FakePlatform()

    def llm_factory(model=None):
        return DeepSeekLLM(
            api_key=api_key,
            model=args.model,
            base_url=args.base_url,
            temperature=0.3,
            max_tokens=8192,
            debug=bool(os.environ.get("AGENT_DEBUG")),
        )

    env = {
        "CTFY_TOKEN": "local-probe-token",
        "LLM_FLAVOR": "openai",
        "LLM_API_KEY": api_key,
        "LLM_MODEL": args.model,
        "LLM_BASE_URL": args.base_url,
        "CTFY_PLATFORM_URL": "https://ctfy.io",
        "CTFY_EVAL_MAX_TURNS": str(args.turns),
        "AGENT_MAX_CALLS_PER_STEP": "6",
        "PYTHONIOENCODING": "utf-8",
    }
    code = run_agent(
        task_path=local_task,
        work_dir=work_dir,
        env=env,
        platform=platform,
        llm_factory=llm_factory,
        result_path=result_path,
    )

    print(f"\n[exit={code}] 平台调用：{[n for n, _ in platform.calls]}")
    if result_path.exists():
        rollup = json.loads(result_path.read_text(encoding="utf-8"))
        print("stop_reason:", rollup.get("stop_reason"))
        print("solved:", rollup.get("solved"), "fully_solved:", rollup.get("fully_solved"))
        print("turns:", rollup.get("turns"), "tool_calls_made:", rollup.get("tool_calls_made"))
        print("writeup 前 400 字:\n" + str(rollup.get("writeup") or "")[:400])
    if platform.report:
        print(f"报告长度: {len(platform.report)} 字")
        print("报告前 600 字:\n" + platform.report[:600])
    return code


if __name__ == "__main__":
    sys.exit(main())
