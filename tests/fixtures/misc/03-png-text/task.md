# 03 图片文本块

附件 `files/pixel.png` 只有 1 个像素，肉眼什么也看不出来。
flag 藏在 PNG 的文本块（tEXt）里，且做过一次十六进制编码。

提示：按 PNG 的 chunk 结构逐块读，找到 tEXt 后取 `关键字\0值` 里的值。
flag 格式为 `FLAG{...}`。
