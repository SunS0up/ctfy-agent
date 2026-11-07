#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T4：给 tests/smoke.py 追加的 misc 扩展工具用例（并入用，不单独运行）。

并入方式（两步）：
  1. 把本文件放到包内 `tests/smoke_case_misc.py`；
  2. 在 `tests/smoke.py` 的 `main()` 里，`cases = [...]` 列表末尾追加：
         ("misc_ext", case_misc_ext),
     并在文件顶部 import 区追加：
         from smoke_case_misc import case_misc_ext

判定内容（补 `case_misc_contract` 没覆盖的运行时行为）：
  - 三个工具都在 `LocalTools.names()` 里（挂载没漏）；
  - `decode_blob` 经框架 `call()` 能解出 flag（不是只有直连模块才work）；
  - 超长输入不突破 6000 字符可见上限；
  - 恶意 zip 的越界成员不会落到目标目录外（Zip Slip 实时验证，不只看源码关键字）；
  - 加密 zip 不会裸抛 RuntimeError，而是记为「加密」跳过（回归：曾抛 "encrypted, password required"）；
  - 未知参数被 `call()` 过滤后仍正常返回（不 TypeError）。
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path


def _ok(label: str) -> None:
    """复用 smoke.py 的 ok()，让本用例计入 PASSED 总数；单独运行时退化成 print。"""
    main = sys.modules.get("__main__")
    reporter = getattr(main, "ok", None)
    if callable(reporter):
        reporter(label)
    else:
        print(f"PASS {label}")


def case_misc_ext(tmp: Path) -> None:
    import base64

    from ctfy_agent.tools_local import LocalTools, ToolError

    work = tmp / "misc_ext"
    work.mkdir(parents=True, exist_ok=True)
    tools = LocalTools(work, "misc-01", allow_shell=True)  # noqa: F841  构造即校验不抛

    names = tools.names()
    for name in ("decode_blob", "unpack_archive", "hexpeek"):
        assert name in names, f"{name} 未挂进 LocalTools.specs()：{sorted(names)}"

    # 1) 经框架 call() 解一层 base64（走的是真实分发路径，不是直连模块）
    payload = base64.b64encode(b"flag{via_local_call}").decode()
    out = tools.call("decode_blob", {"text": payload})
    assert "flag{via_local_call}" in out, out[:200]

    # 2) 未知参数必须被过滤（回显「已忽略」），不能崩
    out2 = tools.call("decode_blob", {"text": payload, "__bogus__": 1})
    assert "flag{via_local_call}" in out2, out2[:200]

    # 3) 超长输入：返回长度不得突破 6000（框架会静默截断，工具自己就得收口）
    big = base64.b64encode(b"A" * 40000).decode()
    out3 = tools.call("decode_blob", {"text": big})
    assert isinstance(out3, str) and len(out3) <= 6000, len(out3)

    # 4) Zip Slip 实时验证：构造越界成员，确认没有文件落到 out_dir 之外
    evil = work / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../escaped.txt", "pwn")
        zf.writestr("ok.txt", "fine")
    out_dir = work / "unzipped"
    tools.call("unpack_archive", {"path": str(evil), "out_dir": str(out_dir)})
    escaped = (work.parent / "escaped.txt").exists() or (work / "escaped.txt").exists()
    assert not escaped, "Zip Slip 未拦住：越界成员被解到了目标目录之外"
    assert (out_dir / "ok.txt").is_file(), "正常成员应当被解出"

    # 6) 加密 zip：不得裸抛 RuntimeError（回归：曾抛 "encrypted, password required"，
    #    会中断 agent 本轮）。夹具 = 真实加密 zip 的 base64（ctf-wiki 2017-HBCTF-flag6，
    #    Python stdlib 写不出加密 zip，故内嵌字节）。
    import base64 as _b64
    enc = work / "enc.zip"
    enc.write_bytes(_b64.b64decode(
        "UEsDBBQAAAgIAFiBhEpdmk2cCAAAAAYAAAALAAAAZmxhZzbkvY3mlbCzNDUxsrAAAFBLAQI/"
        "ABQAAQgIAFiBhEpdmk2cCAAAAAYAAAALACQAAAAAAAAAIAAAAAAAAABmbGFnNuS9jeaVsAoAIAAAAAAA"
        "AQAYAFwhm/YardIB2YYtehmt0gHZhi16Ga3SAVBLBQYAAAAAAQABAF0AAAAxAAAAAAA="
    ))
    out5 = tools.call("unpack_archive", {"path": str(enc), "out_dir": str(work / "enc_out")})
    assert "加密" in out5 and "RuntimeError" not in out5, out5[:300]

    # 5) hexpeek 基本形状（偏移 8 位 + hex + ascii 双栏）
    blob = work / "sample.bin"
    blob.write_bytes(bytes(range(64)))
    out4 = tools.call("hexpeek", {"path": str(blob), "length": 32})
    assert "00000000" in out4 and "89 50" not in out4 and "|" in out4, out4[:160]

    # 7) A1 回归：纯英文文本必须判「未识别」，不能产出假 rot13 链
    out6 = tools.call("decode_blob", {"text": "the quick brown fox jumps over the lazy dog"})
    assert "未识别出可解编码层" in out6, out6[:200]

    # 8) A5 回归：非法整数参数回可读提示，不裸抛 ValueError
    out7 = tools.call("decode_blob", {"text": "abc", "max_layers": "abc"})
    assert "max_layers 非法" in out7, out7[:200]

    # 9) A2 回归：相对路径锚定到沙盒根（hexpeek 相对路径能读到沙盒内文件）
    (work / "rel.bin").write_bytes(b"\x41" * 8)
    out8 = tools.call("hexpeek", {"path": "rel.bin", "length": 8})
    assert "rel.bin" in out8, out8[:160]

    # 10) A9 回归：沙盒外绝对路径必须被拒绝
    try:
        tools.call("hexpeek", {"path": str(work.parent / "outside.bin")})
        raise AssertionError("A9：沙盒外路径未被拒绝")
    except ToolError as exc:
        assert "越界" in str(exc), str(exc)

    _ok("misc_ext：三工具挂载 + call 分发 + 6000 上限 + Zip Slip 拦截 + 加密 zip 跳过 + hexpeek 格式 + 负路径回归")
