#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M3：AI 安全离线夹具验收，并入 tests/smoke.py 使用。

并入方式：
  1. `tests/smoke_ai_fixtures.py`；
  2. smoke.py 顶部 `from smoke_ai_fixtures import case_ai_fixtures`；
  3. `cases` 列表里加 `("ai_fixtures", case_ai_fixtures)`。

每道夹具 = 一个 mock 靶子（server.py）+ 一个确定性参考解（solve.py），
参考解必须**用真 chat 工具**从靶子手里拿到值——证明「工具面 + 打法卡」这个组合
在真实题型上成立，而不是只有提示词写得漂亮。

判定项（防自欺）：
  - 目录结构完整（task.md / server.py / solve.py / expected.txt）；
  - 答案是自然语言值（不是 FLAG{...}）：与真实 AI 题的判分形态一致；
  - solve.py 不读 expected.txt（没作弊）、确实调了 chat；
  - server.py 只用标准库（镜像里没有第三方包）；
  - 子进程真跑两遍都 exit 0 且 stdout 含期望值（确定性）；
  - 输出里的 `[asi] ASIxx` 与目录编号一致（夹具 ↔ 打法卡同源）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "ai"
FLAG_RE = re.compile(r"[A-Za-z0-9_]{2,}\{[^}\s]{4,}\}")


def _ok(label: str) -> None:
    """复用 smoke.py 的 ok()，让本用例计入 PASSED 总数；单独运行时退化成 print。"""
    main = sys.modules.get("__main__")
    reporter = getattr(main, "ok", None)
    if callable(reporter):
        reporter(label)
    else:
        print(f"PASS {label}")


def case_ai_fixtures(tmp: Path) -> None:
    """三道 AI 安全夹具：结构 / 防作弊 / 标准库 / 确定性 / 卡片对齐。"""
    if not FIXTURES.is_dir():
        _ok("AI 夹具：目录为空 → 跳过")
        return

    dirs = sorted(p for p in FIXTURES.iterdir() if p.is_dir())
    assert len(dirs) >= 3, f"AI 夹具应至少 3 道（ASI01/02/06），实有 {len(dirs)}"

    checked: list[str] = []
    for case_dir in dirs:
        name = case_dir.name
        solve_py = case_dir / "solve.py"
        server_py = case_dir / "server.py"
        task_md = case_dir / "task.md"
        expected_py = case_dir / "expected.txt"
        for required in (solve_py, server_py, task_md, expected_py):
            assert required.is_file(), f"{name} 缺 {required.name}"

        # 1) 答案是自然语言值（真实 AI 题不判 FLAG{...} 形态；这条把差异钉住）
        expected = expected_py.read_text(encoding="utf-8").strip()
        assert expected, f"{name}/expected.txt 为空"
        assert not FLAG_RE.search(expected), f"{name} 用了 FLAG{{...}} 形态，与真实 AI 题判分不符"

        # 2) 防作弊：solve.py 不能读答案文件，且必须真调框架工具（对话题用 chat，陷阱页用 http_request）
        source = solve_py.read_text(encoding="utf-8")
        assert "expected.txt" not in source, f"{name}/solve.py 读了 expected.txt（等于没验证）"
        assert "LocalTools" in source, f"{name}/solve.py 没用框架工具层"
        assert ("chat(" in source) or ("http_request(" in source), (
            f"{name}/solve.py 既没调 chat 也没调 http_request（证明不了工具路径）"
        )

        # 3) server.py 只用标准库（镜像里没有第三方包）
        server_src = server_py.read_text(encoding="utf-8")
        for banned in ("import requests", "import openai", "from openai", "import httpx", "import litellm"):
            assert banned not in server_src, f"{name}/server.py 引了第三方包：{banned}"

        # 4) 子进程真跑两遍：确定性 + 退出码 + 期望值 + ASI 编号对齐
        # 目录名形如 03-asi06-memory-poison / 04-asidef-trap-page：
        # 序号管排序，asiNN/asidef 段管题型（两者不一定同号）。
        part = name.split("-")[1]
        asi = "ASI-DEF" if part == "asidef" else "ASI" + part[-2:]
        for run in (1, 2):
            proc = subprocess.run(
                [sys.executable, "solve.py"],
                cwd=str(case_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            assert proc.returncode == 0, f"{name} 第 {run} 遍退出码 {proc.returncode}：{proc.stderr[-400:]}"
            assert expected in proc.stdout, f"{name} 第 {run} 遍没输出期望值 {expected}：{proc.stdout[-300:]}"
            assert f"[asi] {asi}" in proc.stdout, f"{name} 第 {run} 遍 ASI 编号不对（应 {asi}）：{proc.stdout[:200]}"
            # 5) 防诱饵（陷阱页夹具的关键判据）：输出里不许带任何 FLAG{...} 形态的诱饵
            assert not FLAG_RE.search(proc.stdout), (
                f"{name} 第 {run} 遍输出了 FLAG{{...}} 诱饵（说明上了诱饵的当）：{proc.stdout[:300]}"
            )
        checked.append(f"{name}→{asi}")

    _ok(f"AI 夹具：{len(dirs)} 道全绿（结构/防作弊/标准库/双跑确定性/卡片对齐：{', '.join(checked)}）")


if __name__ == "__main__":
    import traceback

    try:
        case_ai_fixtures(ROOT / "dist" / "_ai_fixtures_standalone")
        print("OK")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
