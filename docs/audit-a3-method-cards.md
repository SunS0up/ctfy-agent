# A3 题型方法卡 — 实装审计报告

> 2026-10-25 · 审计对象：A3 落地涉及的 5 个文件 · 规则同前次（先核实后立案、P1-P4 分级、修复逐项验证）
> 修复后回归：`compileall` 干净 · `tests/smoke.py` **51 passed / 0 failed**（真实退出码 0）

## 0. 审计对象（本轮改动清单）

| 文件 | 改动 |
|---|---|
| `src/ctfy_agent/method_cards.py` | **新增**：23 张卡 / 74 键（全部来自实测词表）+ `method_cards()` / `is_asi_tag()` + `MAX_CARDS=8` |
| `src/ctfy_agent/solver.py` | 3 处：import 一行；新增 `_method_hint()`（category==web 门控）；`_role_context` 与 `_initial_prompt` 各追加注入点 |
| `tests/smoke.py` | 新增 `case_a3_method_cards`（7 组断言）并注册；基线 50 → **51** |
| `tests/fixtures/web/tag_vocab.json` | **新增**：50 题实测 tag 快照（含逐题 tags，smoke 漂移守卫用） |
| `runs/harvest_tags.py` | fixture 字典补 `tags_by_challenge` 字段 |

## 1. 程序化审计结果（runs 临时脚本，已删）

| # | 检查 | 结果 |
|---|---|---|
| 1 | 死键：CARDS 74 键 ⊆ 119 实测 tag | ✅ 死键 **0**、跨卡重复键 **0** |
| 2 | 最坏注入 | ✅ **5 张 / 198 字符**（048、035、036 并列）≈ 86 tokens，16k 窗口占比 **0.54%** |
| 3 | AI 泄漏：`AI_TAGS` 是否落在非 AI 题 | ✅ AI 族恰好 10 道（038-047），**0** 误跳风险 |
| 4 | 未映射 tag 分解 | ✅ 45 = ignored 4 + aXX 10 + asiXX 10 + AI_TAGS 21 + **other 0** |

035 端到端注入 5 张，攻击类在前、技术栈垫底，与设计一致。

## 2. 设计正确性复核（代码走查结论）

- **注入面为零**：卡片文案全部来自 `CARDS` 固定表；`tags` 只做集合交集（`wanted.intersection(keys)`），
  tag 值**从不**插进输出文本。未知 tag 只是匹配不上 → 空列表 → 不注入。
  故 `_method_hint` 无需 `_untrusted` 前缀（与题面经 `_untrusted` 的处理形成对照，依据不同）。
- **门控**：`(category or "").strip().lower() != "web"` —— None / "Web" / " web " 均正确处理；misc/rev 不注入。
- **顺序确定性**：输出序 = 表序，与 tag 输入顺序无关（smoke 断言 `reversed(tags)` 结果相等）。
- **同卡去重**：迭代的是卡片条目而非 tag，一卡多键（a07+authentication_failures）只出一次。
- **无循环 import**：`method_cards` 零包内依赖；`solver → method_cards` 单向。
- **与既有护栏无冲突**：卡片文案不含 `report`/`PLAN`/`evidence`/`[[`/`submit_answer`
  （`case_web_playbook` 的五条禁字面全过）；不触碰 `roles.web.system`（v3.1 原文未动）。
- **两处注入点**与批准方案一致（crew 专家 `_role_context` + 单 agent `_initial_prompt`），协调者不注入。
- **kit 不破**：`make_misc_kit.py` 的 `COPY_DIRS` 整树拷贝 `src`/`tests`，
  `method_cards.py` 与 `tests/fixtures/web/` 自动进 kit；`--verify` 判据是 `"0 failed"`（最小值语义），51 ≥ 基线。

## 3. 发现与处置

### P1（必须修）/ P2（应该修）：**无**

### P3（修了，逐项验证）

| # | 位置 | 问题 | 处置 | 验证 |
|---|---|---|---|---|
| 1 | `method_cards.py` `MAX_CARDS` 注释 | 写「实测最坏 6 张（036）」——实测是 **5 张**（048/036/035 并列），数字失实（违反「写数字前核实」） | 改为实测值「5 张 / 198 字符，048、035、036 并列」 | compileall + smoke 51/0 |
| 2 | `tools/make_misc_kit.py:70` | RUN_TESTS 基线写 50，会打进给外包方的 RUN-TESTS.md，数字失真 | 50 → 51 | 同上 |
| 3 | `tools/make_misc_kit.py:292` | KIT-MANIFEST 基线同上 | 50 → 51 | 同上 |

### P4（记录在案，不动）

1. **`software_or_data_integrity_failures`（a08 类目键）挂在反序列化卡上**：024（jwt+path_traversal 主题）会多
   一张邻近主题卡；其「先验签」短语与 024 的 JWT 题面部分契合，且 024 命中 4/8 无截断。保留理由：
   未来若出现「只有 a08 类目 tag、无子 tag」的题，该键有兜底价值；噪声量 ≈ 30 字符，可忽略。
2. **`brute_force`（025，a09 日志题）映射弱凭据卡**：025 的语境是限流/日志，卡片「别无脑爆破」建议无害且合理。保留。
3. **`dist/ai-kit/reference/solver.py` 落后 3 处编辑**（import / `_method_hint` / 两处注入）——该 kit 是另一会话的
   产物且已待交付；外包方交回 AI 角色文件合并时，本来就要按活代码核对 reference，届时一并刷新。
4. **文档旧基线**（HANDOFF 49/49、OUTSOURCING-NOTE 44、misc-brief 47 等）：历史文档不回改；
   本报告为准——**当前基线 51 passed / 0 failed**。全文同步留给下次 PROGRESS 更新，避免本轮改动面扩大。

## 4. 遗留观察（非缺陷）

- AI 安全 10 题（038-047）`category` 全是 `"web"` 且协调者按 category 路由 → 在 ai 角色接入前，
  这类题会派给 web 角色（命中 `jwt_none`/`signature_bypass` 等键时照常给卡，无害）。
  **外包合并时需解决按 tag 路由的问题**（已在 OUTSOURCING-NOTE 上下文记录）。
- `MAX_CARDS=8` 对 23 张卡是「全键齐发」时的截断兜底；实测最坏 5 张，正常永不触发。
- 正赛若出现词表外的新 tag：未命中 → 回退通用 playbook（安全降级）；建议正赛前重跑
  `runs/harvest_tags.py`（零 token）刷新快照，smoke 会自动把新 tag 拦为「未归类」。
