# misc 合并后代码审计报告

**日期**：2026-10-25 ｜ **范围**：misc 模块并入之后的所有改动 ｜ **方法**：diff 基线（`dist/pre-misc-merge/`）锁定变更集 + 全文精读 + 可执行实证（4 组 PoC 全部复现）

**变更集**：`tools_local.py` / `crew.py` / `smoke.py` / `config/agent.json`（4 文件小改，156 diff 行）+ 新增 `tools_misc_ext.py`、`smoke_case_misc.py`、`roles/`、`tests/fixtures/misc/`、`tools/{bench_misc_bank,make_misc_kit,misc_contract_selftest}.py`

---

## 总览

| 级别 | 数量 | 说明 |
|---|---|---|
| **P1（必修）** | 3 | rot13 假链、CWD 错位、stale 分发包 |
| **P2（应修）** | 6 | 错误指引、参数裸穿、bench 魔数、预算重置、白名单不一致、路径无约束 |
| **P3/P4（可选）** | 10 | 冗余与微优化 |

**结论先行**：核心安全闸（Zip Slip/炸弹/链接）质量高、实测有效；问题集中在**边界外的行为**（纯文本输入、相对路径、错误契约）和**产物一致性**（stale kit）。

---

## P1（必修）

### A1｜decode_blob 对纯拉丁文本产生假「rot13×N」解码链 ⚠️ 实证坐实

`tools_misc_ext.py:127-138,141-160`

**机理**：`_text_variants` 只要 rot13 结果 ≠ 原文就列为候选；rot13 后仍是全可打印 ASCII → ratio≈1.0 恒过 0.85 门槛；**没有「结果更像英文」的判据** → 振荡剥壳。

```
>>> decode_blob('the quick brown fox jumps over the lazy dog')
解码链：rot13 → rot13 → rot13 → rot13 → rot13（共 5 层）
最终内容：gur dhvpx oebja sbk whzcf bire gur ynml qbt   ← 乱码
```

**影响**：模型把附件里的普通英文（README/注释/报错回显）喂进来，得到误导性输出，烧一步还带偏方向。讽刺的是 misc 提示词的手动路径都写了「ROT13 只有结果出现可读英文才采信」，工具反而没有这个闸。
**修法**：rot13/url/html 候选仅在命中 flag 或英文单词率显著提升时接受；补回归 `decode_blob(纯英文) → "未识别出可解编码层"`。
**为何没被抓**：`case_misc_ext` 只测真编码输入；bench 8 题无纯英文文本附件——测试盲区。

### A2｜misc 三工具相对路径按进程 CWD 解析，与 shell 沙盒 CWD 错位

`tools_local.py:574`（shell cwd = `store.run_dir/sandbox/<cid>`）vs `tools_misc_ext.py` 全部相对解析（进程 CWD＝/ctfy 或项目根，全库无 `chdir`）。

**影响**：misc 提示词教模型用 `work/a.bin` 相对路径——shell 里能写能读，`hexpeek("work/a.bin")` 却报「文件不存在」，且错误指引还指向不存在的工具（见 A4）。模型要烧 2-3 步才学会改用绝对路径。
**修法**：`LocalTools` 三个适配器把相对路径锚到 `store.run_dir/sandbox/<cid>/` 再转发。

### A3｜`dist/misc-kit.tar.gz` 是 stale 产物（不含 P1 修复）

实证：kit 构建 **15:21:25**，flag_bits 修复 **16:06:39**；`dist/misc-kit/src/.../tools_misc_ext.py` 里 grep `flag_bits` = 0 命中。

**影响**：该分发包携带「加密 zip 裸抛 RuntimeError」旧 bug，kit 内 tests 也没有回归（49 项旧版）。外包流程已结束，kit 的存在意义已消失。
**修法**：**删除** `dist/misc-kit/` + `dist/misc-kit.tar.gz`（重建无意义——那是给接单方的，模块已交回合并）。

---

## P2（应修）

| # | 发现 | 位置 | 说明 |
|---|---|---|---|
| A4 | 错误指引引用**不存在的工具 `run_python`** | tools_misc_ext.py:341,393 | 模型照指引调用 → 「未知本地工具」再烧一步。与 web 提示词的 read_file/write_file 同类「接口对不上」病 |
| A5 | 非法参数裸穿 ValueError ⚠️实证 | :168 `int(max_layers or 5)`、:343 `int(max_depth or 3)`、:396-397 `int(offset or 0)` | 违反模块自己的契约（:164 注释「参数非法一律回可读提示」）。`offset="abc"` 三工具全裸抛。附带：`max_layers=0`→5、`offset=True`→1（无害但怪） |
| A6 | bench 魔数比对永不匹配 gzip/bz2/xz ⚠️实证 | bench_misc_bank.py:44 | `raw[:4]`（恒 4 字节）`in (b"\x1f\x8b", b"BZh", b"\xfd7zXZ")`（2/3/5 字节）恒 False → 只有 zip 被认出。本轮没踩到（题库无裸 gz/xz 归档），下次跑会漏测三条解压路径 |
| A7 | 解包预算**按层重置** | :351-372 walk 每层新建 `_Sink` | 64MB/2000 文件是「每层」上限，最深 5 层 → 理论 320MB 落盘。「炸弹防护」宣传与实现不符 |
| A8 | 广播面过滤了、执行面没过滤 | tools_local.py:265 vs :284 | crew 按角色 local 过滤 SPEC（web/rev 看不到 misc 工具），但 `call()` 白名单=specs()=全量 → 幻觉调用仍会执行。shell 有 allow_shell 硬闸（:282）的先例，misc 三工具没有 |
| A9 | 路径零约束 | hexpeek/unpack_archive 全收绝对路径 | 容器内 `hexpeek("/etc/passwd")` 任意读、`out_dir="/" ` 任意写。对比 http 有端点白名单。与 A2 一并修：统一锚沙盒根+越界拒 |

---

## P3/P4（可选）

| # | 类型 | 内容 |
|---|---|---|
| B1 | 冗余 | `MISC_EXT_SPECS`(:481) 无人消费，死代码 |
| B2 | 冗余 | fixtures 混入 `solve.cpython-312.pyc`×3（二进制产物进仓库；make_misc_kit 会滤掉，但仓库里是垃圾） |
| B3 | 冗余 | `unpack_archive` :375 二次 `_detect(src)`（walk 已判过，重复读 512B） |
| B4 | 冗余 | 参数双重过滤：`call()` 按适配器签名滤一次，`call_ext` 再滤一次 |
| B5 | 过时 | make_misc_kit 基线数字漂移：manifest 写 47(:292)、RUN_TESTS 写 49(:70)、实际 50 |
| B6 | 风险 | `inbox/misc-module/` 是未修复版全套副本（含 run_python、无 flag_bits）——外包已结束，建议标注「历史档案」或移除 |
| B7 | 边界 | 空 `out_dir=''` 产生垃圾目录 `-1` ⚠️实证（`_unique_dir(Path(''))` 所致） |
| C1 | 算法 | `_try_hex` 的 `re.sub(r"0x",...)` 会误伤内部含 0x 的串（"50x1"→"51"） |
| C2 | 算法 | decode_blob 无输入长度上限（7 候选×8 层全量扫；建议 64KB cap） |
| C3 | 算法 | `_extract_zip` 每成员整读进内存 ≤64MB+1（可流式；现有双闸下威胁低） |

---

## 做得好的（审计认账）

- **Zip Slip 四道闸**实测有效（smoke 用例构造 `../../escaped.txt` 实时验证，非只看代码）
- 解压**双闸**：声明大小（file_size/member.size）+ 实际读取上限（`read(MAX+1)` 再验）
- 符号链接/硬链接/目录/设备成员全跳过；加密条目 flag_bits 预判 + base64 内嵌回归夹具
- `_clip` 与 solver 可见上限对齐；`call()` 未知参数过滤+可读回显让模型一步自愈
- make_misc_kit 的泄密扫描（3 正则+跳过标记防误报）与 pyc 清理时序（verify 后清）有真实踩坑注释
- misc_contract 防漂移断言把 live config 拉进回归网

## 测试盲区（A1 的教训）

1. 纯文本/正常输入的**负路径**没有用例（decode_blob 只测了真编码）
2. bench 题库没有 gz/bz2/xz 裸归档 → A6 潜伏
3. CWD 错位在单测里不可见（测试都传绝对路径）——只有真机 run 才会暴露

## 建议修复批次

- **第一批（P1）**：A1 rot13 判据 + 负路径回归；A2 路径锚定沙盒；A3 删 stale kit（一条命令）
- **第二批（P2 攒批）**：A4/A5 错误契约（一次改完 5 处）；A6 bench 魔数；A7 sink 全局化；A8 call() 加闸（与 A2 同一改动面）；A9 随 A2
- **第三批（P3）**：清理 B1/B2/B5/B6 + smoke_case_misc 过时文档头

预计：第一批 ~40 行改动 + 3 条新断言；第二批 ~60 行；第三批纯删除。

---

## 修复记录（2026-10-25 第二轮：核实 + 修复）

已全部落地并跑通 `tests/smoke.py`（**50 passed, 0 failed**，真实退出码 0，非管道伪影）。

| 编号 | 修复 | 文件 | 验证 |
|---|---|---|---|
| A1 | rot13 加「更像英文 / 命中 flag」判据（`_english_hint`），纯英文不再产出假链 | tools_misc_ext.py | smoke 负路径断言 + 实证（纯英文拒绝、flag/句子/单字仍解） |
| A2+A9 | 新增 `_sandbox_dir()`/`_resolve_path()`：相对路径锚定沙盒、越界拒绝；shell 复用同一根 | tools_local.py | smoke 断言 9/10 |
| A3 | 删除 stale 分发包 `dist/misc-kit/` + `misc-kit.tar.gz` | — | Test-Path False |
| A4 | `run_python` → `shell`（2 处错误指引） | tools_misc_ext.py | grep 归零 |
| A5 | 三处 `int()` 裸穿 → 可读提示（max_layers/max_depth/offset/length） | tools_misc_ext.py | smoke 断言 8 + 实证 |
| A6 | bench 魔数 `raw[:4] in (...)` → `raw.startswith(...)` | bench_misc_bank.py | 实证 gzip=True |
| A7 | `_Sink` 预算改为跨层共享 dict | tools_misc_ext.py | 现有解包用例通过 |
| B2 | 删除 fixtures `__pycache__/*.pyc` ×3 | — | 计数 0 |
| B5 | make_misc_kit 基线 47/49 → 50 | make_misc_kit.py | 源码核对 |
| B7 | 空 out_dir 回退 "extracted" | tools_misc_ext.py | 实证（无 `-1`） |

**未修 / 遗留**：

- **A8**（广播面与执行面白名单不一致）：完整修法需在 `solver.py` 的 `run_role`/主循环把角色工具面传给 `LocalTools` 构造——该文件在禁改名单内。A9 路径禁锢已把其实质风险中和（幻觉调用 misc 工具也只能读写本题沙盒）。建议单独排期：给 `LocalTools` 加 `enabled_tools` 参数并在 solver 接线。
- **B1/B3/B4/C1/C2/C3**：已全部处理，见下「第二轮收尾（B/C）」。

---

## 第二轮收尾（B/C 六项，2026-10-25）

价值评估后按「正价值→改、负价值→注释」处理，逐项改完即跑 smoke（均 **50 passed, 0 failed**，退出码 0）：

| 项 | 处理 | 文件 |
|---|---|---|
| B1 | `MISC_EXT_SPECS` 改为**单一事实源**：tools_local 与 crew 都从它派生，删掉 3 处重复 SPEC 导入 | tools_local.py / crew.py |
| B3 | `unpack_archive` 只调一次 `_detect(src)`：`walk` 接收预判类型，头部消息复用 | tools_misc_ext.py |
| B4 | **仅注释**：双重过滤是防御纵深（call() 拦模型幻觉参数 / call_ext 兜独立入口），删层有害 | tools_misc_ext.py |
| C1 | **仅注释**：全局剥 `0x/\x` 是有意放宽（兼容两种混合写法），误判被 printable-ratio 兜底 | tools_misc_ext.py |
| C2 | 加 `DECODE_MAX_CHARS = 256*1024` 上限 + 可读提示 | tools_misc_ext.py |
| C3 | **仅注释**：修正误导的「分块」措辞，说明有界读 ≤64MB 瞬态、流式重构不划算 | tools_misc_ext.py |
