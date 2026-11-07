#!/usr/bin/env python3
"""03 图片文本块的确定性参考解：遍历 PNG chunk，找 tEXt，取十六进制解码后的值。

只用标准库，不联网、不调模型。不做像素解码，只读块结构。
"""
import pathlib
import struct
import sys

SIG = b"\x89PNG\r\n\x1a\n"


def iter_chunks(blob: bytes):
    """按 PNG 规范逐块产出 (类型, 数据)；遇到 IEND 或长度越界即停。"""
    if blob[:8] != SIG:
        raise ValueError("不是 PNG：签名不符")
    pos = 8
    while pos + 12 <= len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        kind = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + length]
        yield kind, data
        if kind == b"IEND":
            return
        pos += 12 + length


def main() -> int:
    png = pathlib.Path(__file__).resolve().parent / "files" / "pixel.png"
    for kind, data in iter_chunks(png.read_bytes()):
        if kind != b"tEXt":
            continue
        keyword, _, value = data.partition(b"\x00")
        print(f"[chunk] tEXt keyword={keyword!r} value={value[:40]!r}", file=sys.stderr)
        try:
            flag = bytes.fromhex(value.decode("ascii")).decode("utf-8")
        except ValueError:
            # 值不是十六进制就原样当文本，避免为一种编码写死
            flag = value.decode("utf-8", "replace")
        print(flag.strip())
        return 0
    print("未找到 tEXt 块", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
