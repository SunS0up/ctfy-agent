#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 `case_misc_contract` 做自测：造 1 份合格交付 + 3 种坏交付，看它该过过、该挂挂。

为什么需要这个
--------------
契约测试本身如果没被验证过，它只是一个「永远 PASS 的装饰」。这里在临时沙箱里
复制一份仓库，模拟接单方交回的五类产物，逐个场景跑 `tests/smoke.py`，断言：

    场景      期望
    clean     契约 case 打印「跳过」（基线不能因为没产物就变红）
    good      PASS（roles + 新工具 + 夹具 全部被校验）
    cheat     FAIL 且报「直接读了 expected.txt」
    noshell   FAIL 且报「allow_shell=true」
    badtool   FAIL 且报「没有同名方法」

用法::

    python tools/misc_contract_selftest.py            # 全部场景
    python tools/misc_contract_selftest.py --keep     # 保留临时目录便于排查
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ROLE_SPEC = {
    "misc": {
        "model": "GLM-Z1-AirX",
        "max_steps": 10,
        "allow_shell": True,
        "max_tokens": 8192,
        "platform": ["read", "instance"],
        "local": ["http_request", "shell"],
        "system": "你是 misc 专家。先用 shell 判型，再逐层处理；卡住就用 report 交卡点。",
    }
}

TOOL_REGISTRATION = '''
DECODE_BLOB_SPEC = {
    "type": "function",
    "function": {
        "name": "decode_blob",
        "description": "识别并逐层剥离 base64/hex。",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}

_specs_orig = LocalTools.specs


def _specs_with_decode(self):
    return _specs_orig(self) + [DECODE_BLOB_SPEC]


LocalTools.specs = _specs_with_decode
'''

TOOL_METHOD = '''

def _decode_blob(self, text: str) -> str:
    return str(text)[:200]


LocalTools.decode_blob = _decode_blob
'''

GOOD_SOLVE = '''import base64, pathlib
blob = pathlib.Path("files/blob.txt").read_text().strip()
print(base64.b64decode(blob).decode())
'''

CHEAT_SOLVE = '''import pathlib
print(pathlib.Path("expected.txt").read_text().strip())
'''

FLAG = "FLAG{nested_b64_ok}"


def _sandbox(name: str) -> Path:
    # 放在工作区内：Windows 沙箱下 `tempfile.mkdtemp()` 落在平台受限临时区，
    # 在其下再建子目录会 PermissionError（WinError 5）。
    base = ROOT / "dist" / f"_selftest_{name}"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    for item in ("src", "tests", "config"):
        shutil.copytree(ROOT / item, base / item)
    for path in base.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)
    return base


def _deliver(box: Path, *, solve: str = GOOD_SOLVE, allow_shell: bool = True, tool: bool = True) -> None:
    roles = box / "roles"
    roles.mkdir(exist_ok=True)
    spec = json.loads(json.dumps(ROLE_SPEC))
    spec["misc"]["allow_shell"] = allow_shell
    (roles / "misc.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    (roles / "misc.system.txt").write_text(spec["misc"]["system"], encoding="utf-8")

    if tool == "spec_only":
        _append(box, TOOL_REGISTRATION)          # 有 SPEC + 注册，没有同名方法
    elif tool:
        _append(box, TOOL_REGISTRATION + TOOL_METHOD)

    fixture = box / "tests" / "fixtures" / "misc" / "01-nested-b64"
    (fixture / "files").mkdir(parents=True, exist_ok=True)
    (fixture / "task.md").write_text("# 多层 base64\n取出 flag。\n", encoding="utf-8")
    (fixture / "files" / "blob.txt").write_text(base64.b64encode(FLAG.encode()).decode(), encoding="utf-8")
    (fixture / "expected.txt").write_text(FLAG + "\n", encoding="utf-8")
    (fixture / "solve.py").write_text(solve, encoding="utf-8")


def _append(box: Path, code: str) -> None:
    target = box / "src" / "ctfy_agent" / "tools_local.py"
    target.write_text(target.read_text(encoding="utf-8") + code, encoding="utf-8")


def _run(box: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "tests/smoke.py"], cwd=str(box),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _evidence_line(output: str, needle: str) -> str:
    """先找含 needle 的行；找不到就退回最后一条非空行（通常是 AssertionError）。"""
    for line in output.splitlines():
        if needle in line:
            return line.strip()
    for line in reversed(output.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="自测 case_misc_contract")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args(argv)

    scenarios = [
        ("clean", None, "跳过", True),
        ("good", {}, "全部通过", True),
        ("cheat", {"solve": CHEAT_SOLVE}, "expected.txt", False),
        ("noshell", {"allow_shell": False}, "allow_shell", False),
        ("badtool", {"tool": "spec_only"}, "同名方法", False),
    ]

    failures: list[str] = []
    for name, options, expect_text, expect_ok in scenarios:
        box = _sandbox(name)
        try:
            if options is not None:
                _deliver(box, **options)
            code, output = _run(box)
            line = _evidence_line(output, expect_text)
            ok_expected = (code == 0) == expect_ok
            text_expected = expect_text in output
            status = "OK " if (ok_expected and text_expected) else "BAD"
            print(f"[{status}] {name:8s} exit={code} | {line[:110]}")
            if not (ok_expected and text_expected):
                failures.append(f"{name}: exit={code} expect_ok={expect_ok} text={expect_text!r} got={line[:160]!r}")
        finally:
            if not args.keep:
                shutil.rmtree(box, ignore_errors=True)
            else:
                print(f"       kept: {box}")

    print()
    if failures:
        print("契约自测失败：", file=sys.stderr)
        for item in failures:
            print("  -", item, file=sys.stderr)
        return 1
    print("契约自测全部符合预期（该过的过、该挂的挂、基线不受影响）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
