#!/usr/bin/env python3
"""02 归档嵌套的确定性参考解：zip → gzip → 明文。

只用标准库，不联网、不调模型；逐个成员尝试解压，命中即停。
"""
import gzip
import io
import pathlib
import sys
import zipfile
import zlib


def try_decompress(blob: bytes) -> bytes | None:
    """判断成员是不是 gzip 流（魔数 1f 8b），是则解压。"""
    if blob[:2] == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=io.BytesIO(blob)).read()
    return None


def main() -> int:
    pkg = pathlib.Path(__file__).resolve().parent / "files" / "package.zip"
    with zipfile.ZipFile(pkg) as z:
        for name in z.namelist():
            raw = z.read(name)
            # 先看魔数，再决定要不要解；扩展名不可信，这里只用魔数
            inner = try_decompress(raw)
            if inner is None:
                print(f"[skip] {name}: 不是 gzip 流", file=sys.stderr)
                continue
            text = inner.decode("utf-8", "replace").strip()
            print(f"[hit] {name} 解压后 {len(inner)} B", file=sys.stderr)
            print(text)
            return 0
    print("未找到可解压成员", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
