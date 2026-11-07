#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""misc 模块零 LLM 检验台（含诊断）。3 工具 × 8 真实题，0 token。"""
from __future__ import annotations

import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ctfy_agent.tools_misc_ext import (  # noqa: E402
    TOOL_TEXT_MAX,
    decode_blob,
    hexpeek,
    unpack_archive,
)

BANK = ROOT / "misc-bank"
WORK = ROOT / "runs" / "_bench_work"
TEXT_EXTS = {".txt", ".md", ""}


def exp_of(slug: str) -> str:
    return (BANK / slug / "expected.txt").read_text(encoding="utf-8").strip()


def files_of(slug: str) -> list[Path]:
    d = BANK / slug / "files"
    return sorted(p for p in d.iterdir() if p.is_file())


def locate(p: Path, needle: str) -> int | None:
    data = p.read_bytes()
    i = data.find(needle.encode("utf-8", "replace"))
    return i if i >= 0 else None


def is_archive(p: Path) -> bool:
    raw = p.read_bytes()
    if raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"BZh", b"\xfd7zXZ")):
        return True
    return len(raw) >= 262 and raw[257:262] == b"ustar"


def ascii_stream(out: str) -> str:
    """把 hexpeek 输出的各 ASCII 列（|...|）拼接，恢复连续可读流。"""
    cols = []
    for line in out.splitlines():
        if "|" not in line:
            continue
        # 格式： offset  hexhexhex  |ascii|
        cols.append(line.split("|")[1])
    return "".join(cols)


class Stats:
    def __init__(self):
        self.d = {n: {"calls": 0, "hits": 0, "errors": [], "ms": 0.0, "max_out": 0}
                  for n in ("decode_blob", "unpack_archive", "hexpeek")}

    def rec(self, name, out, t0, err=None):
        self.d[name]["calls"] += 1
        self.d[name]["ms"] += (time.perf_counter() - t0) * 1000
        self.d[name]["max_out"] = max(self.d[name]["max_out"], len(out))
        if err:
            self.d[name]["errors"].append(err)


def run_decode(slug, exp, S):
    fed = []
    head = ""
    for p in files_of(slug):
        if p.suffix.lower() not in TEXT_EXTS:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        fed.append(p.name)
        t0 = time.perf_counter()
        try:
            out = decode_blob(text); err = None
        except Exception as e:
            out, err = "", f"{type(e).__name__}: {e}"
        S.rec("decode_blob", out, t0, err)
        if err:
            return f"ERROR {err}", f"fed={fed} head={head!r}"
        if exp in out:
            S.d["decode_blob"]["hits"] += 1
            return f"RECOVERED via decode_blob({p.name})", f"fed={fed}"
        if not head:
            head = text[:90]
    return "not-recovered", f"fed={fed} head={head!r}"


def run_unpack(slug, exp, S):
    results = []
    for p in files_of(slug):
        if not is_archive(p):
            continue
        out_dir = WORK / slug / p.stem
        t0 = time.perf_counter()
        try:
            out = unpack_archive(str(p), out_dir=str(out_dir)); err = None
        except Exception as e:
            out, err = "", f"{type(e).__name__}: {e}"
        S.rec("unpack_archive", out, t0, err)
        if err:
            results.append(f"{p.name}: {err}")
            continue  # 继续试下一个归档，不中断
        if exp in out:
            S.d["unpack_archive"]["hits"] += 1
            return f"RECOVERED via unpack_archive({p.name}) output", "; ".join(results)
        for f in sorted(out_dir.rglob("*")):
            if f.is_file():
                try:
                    c = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if exp in c:
                    S.d["unpack_archive"]["hits"] += 1
                    return f"RECOVERED via unpack_archive({p.name})→{f.name}", "; ".join(results)
    return "not-recovered", "; ".join(results)


def run_hexpeek(slug, exp, S):
    for p in files_of(slug):
        idx = locate(p, exp)
        if idx is None:
            continue
        off = max(0, idx - 16)
        length = min(len(exp) + 32, 1024)
        t0 = time.perf_counter()
        try:
            out = hexpeek(str(p), offset=off, length=length); err = None
        except Exception as e:
            out, err = "", f"{type(e).__name__}: {e}"
        S.rec("hexpeek", out, t0, err)
        if err:
            return f"ERROR {err}", f"{p.name} @{idx}"
        stream = ascii_stream(out)
        if exp in stream:
            S.d["hexpeek"]["hits"] += 1
            return f"RECOVERED via hexpeek({p.name} @{idx})", f"plain@{idx}"
        return f"located@{idx} but ASCII列跨行断裂", f"{p.name} plain@{idx}"
    return "not-recovered", "flag 非明文存于附件"


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    S = Stats()
    rows = []
    for slug in sorted(d.name for d in BANK.iterdir() if d.is_dir()):
        exp = exp_of(slug)
        dv, ddiag = run_decode(slug, exp, S)
        uv, udiag = run_unpack(slug, exp, S)
        hv, hdiag = run_hexpeek(slug, exp, S)
        ok = "RECOVERED" in (dv + uv + hv)
        rows.append((slug, exp, dv, uv, hv, ok, ddiag, udiag, hdiag))

    print("=" * 104)
    print("misc 模块 · 零 LLM 检验报告（decode_blob / unpack_archive / hexpeek × 8 真实题）")
    print("=" * 104)
    for slug, exp, dv, uv, hv, ok, ddiag, udiag, hdiag in rows:
        print(f"\n{'✅' if ok else '➖'} {slug}")
        print(f"   答案 {exp}")
        print(f"   decode_blob    {dv:40s}  {ddiag}")
        print(f"   unpack_archive {uv:40s}  {udiag}")
        print(f"   hexpeek        {hv:40s}  {hdiag}")

    solved = sum(1 for r in rows if r[5])
    print("\n" + "=" * 104)
    print(f"覆盖率：{solved}/8 由 3 个专用工具直接解出；其余 {8 - solved}/8 需 shell/额外手段（见下）")
    print("=" * 104)
    for name, s in S.d.items():
        print(f"  {name:15s} 调用 {s['calls']:3d}  命中 {s['hits']:3d}  错误 {len(s['errors']):2d}  "
              f"耗时 {s['ms']:7.1f}ms  最大输出 {s['max_out']} 字符（上限 {TOOL_TEXT_MAX}）")
    errs = [(n, e) for n, s in S.d.items() for e in s["errors"]]
    if errs:
        print("\n  ⚠️ 错误/裸抛明细：")
        for n, e in errs:
            print(f"    [{n}] {e}")

    shutil.rmtree(WORK, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
