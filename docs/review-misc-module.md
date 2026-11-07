# misc 模块交付包验收报告

**包**：`dist/misc-module.tar.gz`（53.1 KB，28 个文件，SHA256 `FFF725E0…A2602D3`）
**解压位置**：`inbox/misc-module/`（只读检查，**未合并进仓库**）
**核验方式**：静态审计 + 独立解码复核 + **一次性沙箱实跑**（`dist/_verify_misc/`，已删除风险为零）

## 结论

**可以用，四条验收标准全部真实满足**（我在沙箱里自己跑出来的，不是采信他们的 README）：
`compileall` OK、`49 passed, 0 failed`、契约用例 **7 项全过**、3/3 夹具 `solve.py` 复现 `expected.txt`。

但有 **1 个必须补的缺口（P0）**：`roles/misc.json` 装进去了，**框架不会读它**——不合并进
`config/agent.json` 的话，真跑 Crew 时用的仍是旧的 misc 角色（`allow_shell:false`、无 system 提示词），
等于装了个空壳。

## 一、独立核验矩阵（左列是我的实测，右列是他们的自述）

| 核验项 | 我的实测方法 | 结果 |
|---|---|---|
| 基线没被踩 | 比 `install.py` 声明的 sha256 与当前 `crew.py`/`tools_local.py` | ✅ **两个文件哈希完全一致** |
| 改动只落在允许范围 | 逐 hunk 应用 patch，结果与 `*.new.py` 逐字节比对 | ✅ 完全相等（两处 +22 / +16−1 行） |
| 没丢我们已有工作 | 在 `crew.new.py` 里查 `EVIDENCE_RULE`/`COMPANION_RULE`/`HANDOFF_RULE`/`_delegate_task`/`PlanTrack` | ✅ 全在；`tools_local.new.py` 保留 `REPORT_SPEC`+evidence |
| 沙箱实跑 | 复制仓库 → 跑他们 `install.py` → 直接跑 `tests/smoke.py` | ✅ **49 passed, 0 failed** |
| 契约用例 | 同上，看「外包接入契约」行 | ✅ **7 项**（roles + 3 tool + 3 fixture） |
| 新用例 | `misc_ext` 用例 | ✅ PASS（含 Zip Slip 实时拦截、6000 上限、未知参数过滤） |
| 夹具能否复现 | 在沙箱 `cd tests/fixtures/misc/0X && python solve.py` | ✅ 3/3 输出含 `expected.txt` |
| 夹具 ground truth 真假 | **我自己的解码器**（不跑他们的 solve.py） | ✅ 三个 flag 逐字相同：`n3st3d_l4y3r5_4r3_fun` / `n0t_h1dd3n_ju5t_p4ck3d` / `t3xt_chunk_h1d3s_th3_fl4g` |
| 抗朴素搜索 | 01 是 hex 链、02 是 gz 内层、03 是 PNG tEXt 里 hex 编码 | ✅ 字面 `FLAG{` 直接 grep 都搜不到 |
| 依赖 | ast 扫 10 个 .py 的全部 import | ✅ **0 个非标准库** |
| 危险调用 | 扫 `eval/exec/os.system/socket/subprocess(shell=True)/urlopen/读.env` | ✅ 0 命中（唯一 `shutil.rmtree` 在 `install.py`，只删包内夹具目录） |
| 语法 | `ast.parse` 全部 10 个文件 | ✅ 0 错 |
| 提示词工具名可用 | 抽出提示词里的工具名，比对 `PLATFORM_SCOPE[read]+[instance]` 的 23 个真名 | ✅ 3/3 都在面内 |
| role 字段合法 | `allow_shell=true`；`max_steps=10`；`max_tokens=8192`（`crew.py:203` 支持角色级覆盖）；`local` 5 个工具 | ✅ |
| json 与 txt 一致 | 逐字比对 | ✅（3438 vs 3439 字符，差 1 个结尾换行） |
| smoke 注册锚点唯一 | `IMPORT_ANCHOR`/`CASE_ANCHOR` 在当前 `tests/smoke.py` 里计数 | ✅ 各 1 次（脚本注入不会错位） |

**提示词质量**（3397 字符）：与我们三条行为契约逐条对齐——evidence 必写「原样连续片段」且注明「被打回最多 2 次」、
明确「同工具同参二次调用会被账本拦截」、写明 16000/6000 上下文口径、明确「没有 file/xxd/unzip/binwalk/zsteg/PIL」、
10 步预算分段。因为文本里已含 "evidence"，我们的 `_role_system` **不会**重复注入 `EVIDENCE_RULE`。

## 二、问题清单

### P0（合并前必须处理）
1. **`roles/misc.json` 不会自动生效**。框架只读 `config/agent.json` 的 `extra.roles`（`crew.py:224`），没有任何
   扫描 `roles/` 目录的代码；`install.py` 也不碰 `config/agent.json`。→ 装完必须手工把 `roles.misc` 合并进
   `config/agent.json`，否则真跑时 misc 仍是「`allow_shell:false` + 无提示词 + 无 3 个新工具」的旧配置。

### P1（建议修，不阻塞）
2. **`install.py` 的验收器在 Windows/GBK 下必挂**：`subprocess.run(..., text=True)` 用系统默认编码解码自己输出的
   UTF-8 中文 → `UnicodeDecodeError` → 打印「未能解析 smoke 结果」并 exit 1。**安装本身是成功的**，只是它的
   自验收挂了。修法 3 行：`encoding="utf-8", errors="replace"`。（我绕过它直接跑 `tests/smoke.py` 拿到 49 passed。）
3. **代码量 708 行 vs 工单红线 ≤400**，他们主动申报豁免（`tools_misc_ext.py` 501 + case 82 + 3×solve 125）。
   这些行**全是纯新增、未触碰框架既有路径**，我倾向接受豁免——需你拍板。

### P2（可留到以后）
4. **加第 4 个工具要改两处**：`tools_local.specs()` 一个列表 + `crew._role_specs()` 里一个硬编码三元组。
   建议照着 `tests/smoke.py` 里 `_outsourced_tools()` 的写法改成「按 `role.local` 名字匹配模块内的 `*_SPEC`」，
   以后新增工具零改框架。
5. **跑分与 27 项自检不可复现**：`VERIFY-REPORT.md` §7 和 `SELFCHECK.md` 引用的 `out/bench/{adapter,harness,report,selfcheck}.py`
   **没随包交付**。它们超出工单范围（属于自选加分项），但既然写了数字就该让数字可复核。
6. **`patches/*.new.py` 是整文件覆盖**，与工单「只交增量」不符。有基线哈希护栏，且我已验证「当前文件 + patch == .new.py」，
   所以**等价安全**；但正式合并我建议走 `.patch`，不用 `.new.py`。

## 三、他们值得记一笔的地方

- 跑分出现**负结果**（带工具 4/7 vs 基线 5/7）时没有粉饰，而是定位到步级、指出「工具降低了直搜概率」并把
  C 组**主动作废**（因为自己引入了一条「3 步无进展就换附件」的坏规则）。这种自觉性比多解一道题值钱。
- `case_misc_ext` 里做的是**实时 Zip Slip 攻击**（构造 `../../escaped.txt` 再断言没落地），不是 grep 源码关键字。
- 夹具三道刻意覆盖三种不同载体（编码链 / 归档嵌套 / 图片元数据），且 flag 都规避了字面搜索。

## 四、建议的合并步骤（等你批准，我还没动仓库）

```powershell
# 1) 用 patch 改两处框架文件（等价于他们的 .new.py，但增量可控）
#    tools_local.py: +import +3 SPEC +3 方法；crew.py: import + _role_specs 三元组
# 2) 纯新增文件
#    src/ctfy_agent/tools_misc_ext.py
#    roles/misc.json  roles/misc.system.txt
#    tests/smoke_case_misc.py
#    tests/fixtures/misc/01..03/
# 3) 注册用例（两行，锚点唯一）
#    tests/smoke.py: import + ("misc_ext", case_misc_ext)
# 4) 【P0】把 roles/misc.json 的 roles.misc 合并进 config/agent.json
# 5) 验收
python -m compileall -q src tests        # 0 error
python tests/smoke.py                    # 期望 49 passed, 0 failed（48 + misc_ext）
python tests/smoke.py 2>&1 | findstr 外包接入契约   # 期望「7 项…全部通过」
```

## 五、本次没验证的（诚实标注）

- **Crew 端到端真跑**：需要模型网关 + 预算，且 misc 题池现在是 0 道。他们自建的 3 道夹具可以真跑，但要花
  3×150k 左右 token。
- **他们的跑分结论**：包内没脚本，无法复核；他们自己也说 n=7 单次采样不足以下结论，我同意。
- **`GLM-Z1-AirX` 上的提示词表现**：未验证。提示词 3397 字符 + system 前缀，是否触发上下文压缩、是否
  影响 10 步预算，都要真跑才知道。

## 附：本次产生的文件

- `inbox/misc-module/`——解压后的交付包（只读）
- `dist/_verify_misc/`——沙箱（已装好他们的模块，可直接复跑验证；不需要可整个删掉）
- `runs/inspect_misc_module.py`、`runs/verify_fixtures_truth.py`——核验脚本（可重跑）
