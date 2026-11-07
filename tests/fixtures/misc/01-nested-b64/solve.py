#!/usr/bin/env python3
"""01 多层编码的确定性参考解：hex → rot13 → base64 → 明文。

只用标准库，不联网、不调模型；从附件真解，不看任何答案文件。
"""
import base64
import codecs
import pathlib
import sys

# 三层的剥法，从外到内。顺序靠观察字符集判断：
#   纯 [0-9a-f] 且长度为偶数  → 十六进制
#   含 { } 且 rot13 后可读    → 单表替换
#   含 [A-Za-z0-9+/=] 且长4倍 → base64
LAYERS = (
    lambda s: bytes.fromhex(s).decode("ascii"),      # 十六进制
    lambda s: codecs.decode(s, "rot13"),             # ROT13
    lambda s: base64.b64decode(s).decode("utf-8"),   # Base64
)


def peel(text: str) -> str:
    cur = text.strip()
    for i, step in enumerate(LAYERS, 1):
        cur = step(cur)
        print(f"[layer {i}] -> {cur[:60]}", file=sys.stderr)
    return cur


def main() -> int:
    src = pathlib.Path(__file__).resolve().parent / "files" / "message.txt"
    flag = peel(src.read_text(encoding="utf-8"))
    print(flag)
    return 0 if flag.startswith("FLAG{") else 1


if __name__ == "__main__":
    raise SystemExit(main())
