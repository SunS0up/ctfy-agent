# misc 模块 · 零 LLM 检验报告（含复核 + 修复 + 复测）

> 检验对象：已合并的 misc 模块（`decode_blob` / `unpack_archive` / `hexpeek` 三工具）
> 题库：`misc-bank/`（8 道真实题，来自 ctf-wiki/ctf-challenges）
> 方式：`tools/bench_misc_bank.py` 确定性跑工具，全程 **0 LLM token**（不联网、不调模型）

## 一句话结论

三工具全部稳定跑完（加密 zip 裸抛**已修复**），输出全部远低于 6000 上限、耗时毫秒级；但它们是**三类窄场景加速器**，不是通用 misc 求解器——真题库 8 道里纯工具只直接解出 **1 道**，另有 **2 道**靠 hexpeek 揭示（需 shell 先定位），**5 道**必须 shell + 领域逻辑。

## 覆盖矩阵（8 题）

| 题目 | 纯 3 工具 | 工具 + shell 定位 | 实际所需手段 |
|---|---|---|---|
| archive__2017-HBCTF-flag6 | ✅ | — | `unpack_archive` 解出 `flag6位数`=答案 `954288` |
| cap__2016CFF-Structs2 | ➖ | ✅ hexpeek@810621 | pcap 里 flag 是明文，`grep -abo` 定位 + hexpeek 看 |
| picture__lsb-2016HCTF | ➖ | ✅ hexpeek@1496 | flag 是 ELF `1` 里的明文字符串，`strings` 定位即可 |
| encode__base64-stego | ➖ | ➖ | 数据=base64 编码的 wiki 文章，flag 藏在**隐写**（padding/空白），普通 decode 拿不到 |
| archive__known-plaintext-demo | ➖ | ➖ | demo.zip **加密**，需已知明文攻击（pkcrack 类） |
| picture__gif-2017-xman-100 | ➖ | ➖ | `100_fix.gif` 头 `GIF89`+0x60 一位损坏，需修复头 + 抽帧 |
| picture__metadata-Pretty_Cat | ➖ | ➖ | flag 不在 jpg 任何直接编码字节（UTF-8/16 全无），需 exiftool 解析元数据 |
| picture__png-2014-sctf-misc400 | ➖ | ➖ | PNG 像素 → 坐标解码（答案本身是坐标串） |

**分层覆盖**：纯工具 1/8；工具参与揭示 3/8；纯 shell+领域逻辑 5/8。

## 工具统计（8 题全跑，修复后复测）

| 工具 | 调用 | 命中 | 错误 | 耗时 | 最大输出（上限 6000） |
|---|---|---|---|---|---|
| decode_blob | 2 | 0 | 0 | 16.3 ms | 742 字符 |
| unpack_archive | 4 | 1 | **0** | 7.2 ms | 184 字符 |
| hexpeek | 2 | 2 | 0 | 0.3 ms | 473 字符 |

- 总调用 8 次、总耗时 23.8 ms、0 token。
- 未触发 Zip Slip / 解压炸弹（真实题无恶意构造；护栏已由 `case_misc_ext` 覆盖）。

## 复核 + 修复 + 复测

独立重放（不轻信首轮），5 条结论全部坐实：

1. ✅ P1 成立：`unpack_archive` 解加密 `flag6.zip` 抛 `RuntimeError: ... is encrypted, password required for extraction`。
2. ✅ `info.flag_bits & 0x1` 精确识别加密（加密条目 True、明文条目 False）→ 即修法。
3. ✅ FLAG_RE 盲区：`{FLAG:…}` 与 `954288` 均 None（只认 `前缀{…}`）。
4. ✅ hexpeek 跨行：21 字符 flag 被拆成 `flag{abcdefghijk` + `lmnop}`，子串 `in` 匹配失败。
5. ✅ 新发现：Python stdlib `zipfile` **写不出加密 zip**（`open(w)` 不加密、`pwd=` 仅读用）→ 回归夹具必须内嵌真实加密 zip 的 base64。

修复动作：
- `src/ctfy_agent/tools_misc_ext.py` `_extract_zip`：`is_dir()` 之后加 `if info.flag_bits & 0x1:` → `sink.skipped.append(f"{info.filename}（加密，需密码爆破/已知明文）"); continue`。加密条目在 `zf.open()` 之前被拦下，不再裸抛。
- `tests/smoke_case_misc.py` `case_misc_ext` 增第 6 项回归断言：内嵌 `flag6.zip`（164 字节）base64，断言输出含「加密」且无 `RuntimeError`。

复测结果：
- `python -m compileall -q src tests tools` → 0 error
- `python tests/smoke.py` → **49 passed, 0 failed**（退出码 0）
- `python tools/bench_misc_bank.py` → `unpack_archive` 错误 **1→0**，覆盖率不变（3/8）

## 发现的缺陷

### P1 — `unpack_archive` 解加密 zip 裸抛 RuntimeError（✅ 已修复）
- 现象（修复前）：解加密 zip 抛 `RuntimeError: ... is encrypted, password required for extraction`，未转可读提示，会中断 agent 本轮。
- 根因：`_extract_zip` 直接 `zf.open(info)`，外层 `walk` 的异常元组只含 `(BadZipFile, TarError, OSError)`，不含 `RuntimeError`。
- 修复：加密位（`flag_bits & 0x1`）在解包前拦截 → 记入 `skipped` → 输出「已拦截：flag6位数（加密，需密码爆破/已知明文）」。
- 回归：`case_misc_ext` 第 6 项断言（内嵌真实加密 zip base64）。

### P2 — hexpeek 16 字节分行，跨行 flag 在输出里不连续（暂不修，可选）
- `hexpeek` 按 16 字节/行渲染 ASCII 列，跨行界的 flag 被 `\n`+偏移+hex 列打断，程序化子串匹配会漏。
- 模型能跨行读，影响小；可选增强：输出末尾附一行「连续 ASCII 流」。

### P2 — `decode_blob` 的 flag 正则只认 `前缀{...}`（暂不修，可选）
- `FLAG_RE`（`:40`）不认 `{FLAG:…}`（无前缀）和纯数字答案（无括号）。
- 不影响解码本身（最终内容仍会打印），只影响「命中 flag」高亮判定。

## 结论

1. 工具**质量过关**：8 题全跑零错误（加密 zip 已修）、零越限、零耗时问题；`case_misc_ext` 守住注册/签名/上限/炸弹/加密。
2. **覆盖预期要摆正**：3 工具是「编码剥离 / 安全解包 / hex 预览」加速器，真题库直接命中约 1/8（参与揭示 3/8）。misc 角色已 `allow_shell=true`，5/8 的 stego/元数据/抽帧/解密题本就走 shell 兜底——这与「专项工具加速 + shell 通用兜底」架构一致，不是缺陷。
3. **P1 已修并补回归**；P2 两项可选，暂不动。

复现：`python tools/bench_misc_bank.py`（离线、0 token）。
