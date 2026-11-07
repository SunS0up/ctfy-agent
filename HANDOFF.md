# 交接文档 — TPCTF 2026 智能体赛道

> 给新对话的 agent：先读完这一页，再动手。所有结论都有实测依据，**不要重新推导已经验证过的东西**（会浪费大量 token 和实例时间）。
>
> 只想粘一段话开新对话 → 用 `docs/HANDOFF-2026-10-25.txt`（本文件的纯文本自包含版）。

## 1. 任务

- 目标：TPCTF 2026 **智能体团队赛道**的提交物 = **一个容器镜像**（不是本地 CLI）。
- 契约：平台写只读 `/ctfy/work/task.json`（CTFY-0001 schema v2）→ 我们把结论写成 `/ctfy/work/result.json` 并 `exit 0`。**stdout 对用户镜像不读**（平台自有镜像才有 legacy 兜底，且它把整段 stdout 当一个 JSON 解析）→ 日志一律 stderr，stdout 最多留一行 JSON。
- 计分：解题在**运行期**通过 MCP 的 `submit_answer` 完成，不是赛后判分。
- 合规赛道：**Web / Pwn / AI 安全**；Web 优先，Pwn 时间够才做。
- 代码约束：**纯 stdlib Python**（无第三方依赖），模型走 DeepSeek/Paratera 的 OpenAI 兼容接口。
- 工作目录：`E:\ctfy_project`。

## 2. 仓库地图（关键锚点，行号已核）

| 文件 | 作用 | 关键位置 |
|---|---|---|
| `src/ctfy_agent/crew.py` | 多智能体：协调者 + web/rev/crypto/misc/**ai** 专家 | `_brief` 583（**已输出 facets：难度/已解/tags**）、`_prior_digest` 74（账本为主+结论为辅）、`_delegate_task` 99（**A2：objective+done_when 任务头**）、`COORDINATOR_PROMPT` 115（**含 ai 路由判据：asi/prompt_injection/agent/LLM/助手/bot/聊天/RAG；陷阱页可派 web**）、`COORDINATOR_TOOLS` 130（**delegate 必填 role/objective/done_when，task 仅兼容**）、`_delegate` 257（`ledger=` 透传、`allow_chat` 按角色 local 传）、`_role_system` 287（**追加 COMPANION_RULE**）、`solve` 326（**391 建账本、392 建 plan；delegate 也进账；每次 solve 新建 Solver**）、`PLATFORM_SCOPE` 32-44（含 `list_hints/view_hint`，共 26 项） |
| `src/ctfy_agent/solver.py` | 专家主循环、工具分派、上下文滑窗 | `_ledger_gate` 172 / `_ledger_after` 200（**A1 去重闸；均包 try，归一化异常降级不击穿 run**）、`_check_report` 214（**A2 证据契约**）、`_execute` 354、`solve` 486（单 agent，**入口 `_http_cache.clear()`**）、`run_role` 727（账本摘要+**题型背景**前置、跨 delegate 共享、**A2 plan 注入**）、`_role_context` 932、`_initial_prompt` 950 |
| `src/ctfy_agent/companion.py` | **A2 伴生文本**：`parse_companion` / `PlanTrack` | `parse_companion` 62（标签宽容解析：强调符/全角冒号/列表/多行）、`PlanTrack` 82（`observe` 101 只认「账本新增尝试」作推进信号、`reminder` 136 有配额、`stalled`/`stall_hint` 143/146 每 episode 一次、`stats` 153） |
| `src/ctfy_agent/ledger.py` | **A1 确定性账本 + A2 证据校验** | `normalize_args` 121（URL/body 归一、**delegate 读 `objective`**）、`attempt_key` 146、`looks_failed` 209（http 优先看 status）、`claims_discovery` 227（先摘否定式）、`Ledger` 282（`seen` 315 / `record` 329）、`verify_evidence` 394、`recent_sources` 427、`rotate` 440、`digest` 454、`stats` 487 |
| `src/ctfy_agent/state.py` | 状态持久化 `runs/<run>/state.json` | `Challenge` 141（**已含 `tags/difficulty/solves_count`**）、`from_dict` 172（归一）、`facets()` 181、`upsert` 238（**已合并 tags 并集 + 难度/已解**）、`normalize_tags` 87（宽容解析） |
| `src/ctfy_agent/cli.py` | 子命令 | `normalize_challenge` 65（**已映射三字段；tags 兜底已摘**）、`ensure_challenge_meta` 87（**老 state 补 tags**） |
| `src/ctfy_agent/tools_local.py` | 本地工具 http_request/shell/note/report + misc 三工具薄方法 | `call()` 签名过滤、`_render_http`（窗口 5000） |
| `src/ctfy_agent/tools_misc_ext.py` | **外包 misc 模块**：decode_blob / unpack_archive / hexpeek（纯 stdlib，501 行） | `call_ext` 统一 ≤6000 与 ToolError 语义；Zip Slip / 解压炸弹已拦 |
| `src/ctfy_agent/llm.py` | OpenAI 兼容客户端（urllib） | **`chat` 104-114，`"stream": False`；非流式平台网关也支持（`proxy.py:621,1041`），故 B2 是健壮性项不是阻断项**；只有 `/chat/completions`（`:120`），**不认 `anthropic` flavor** |
| `src/ctfy_agent/platform.py` | MCP 客户端 | 97 个工具暴露；**`MODEL_TOOLS` 55 起**是单 agent 路径的真白名单（26 项，含 `list_hints/view_hint`），`PLATFORM_SCOPE`（crew.py:31-45）只是 crew 的角色分组 |
| `config/agent.json` | 角色/模型/预算 | coordinator+web=Kimi-K2.7、rev=Kimi-K3、**ai=Kimi-K3**、crypto=DeepSeek-V4-Pro-0813、misc=GLM-Z1-AirX；`max_tokens_per_challenge=500000`、`max_llm_calls=80`。**容器里角色 model 会被 `agent_main._build_config` 抹掉，统一用 `LLM_MODEL`** |
| `Dockerfile` | 只有工具链 + COPY src，**无 entrypoint** | 需补 B1/B3 |
| `tests/smoke.py` | 离线回归 | **79/79 通过**（含 AI/M1–M4 五个用例：`ai_chat`/`ai_contract`/`ai_wiring`/`ai_fixtures`/`ai_agent_e2e`），改动后必须保持全绿 |
| `src/ctfy_agent/method_cards.py` | 题型方法卡 | `CARDS`（web 50 题词表）+ **`AI_CARDS` 17 张**（10 ASI + 7 防御族）、`AI_TAGS` 80 个、`method_cards()` AI 卡优先 |
| `roles/ai.json` + `roles/ai.system.txt` | ai 专家角色与提示词（1627 字符） | 契约用例守卫零漂移；提示词避开 `report`/`evidence`/`PLAN` 字面（框架规则自动拼） |
| `tests/fixtures/ai/` | AI 安全离线夹具 + 三池词表快照 | `pool_tags.json`（10+5+37=52 题）由 `case_ai_contract` 对账；4 道夹具由 `case_ai_fixtures` 双跑验证 |
| `tests/smoke_ai_*.py` | AI 专项用例 | `smoke_ai_chat`（chat 工具）/`smoke_ai_role`（契约+接线）/`smoke_ai_fixtures`（夹具）/`smoke_ai_agent`（容器契约 e2e） |
| `docs/ai-plan.md` | AI 安全纵向的计划与逐里程碑证据（M1–M4） | 含审计记录、探针数据、已知边界 |
| `tools/make_misc_kit.py` | misc 外包 kit 生成器 | `--verify` 在 kit 内实跑单测；自带泄密断言（禁 `.env`/`runs/`/`HANDOFF.md`） |
| `tools/misc_contract_selftest.py` | 契约测试的自测 | 沙箱造 5 种交付，断言「该过过、该挂挂」 |
| `roles/` + `tests/fixtures/misc/` | 外包产物落地位（已内置一套 misc） | 由 `case_misc_contract` 校验（7 项）；含 config↔roles 防漂移断言 |
| `docs/misc-handover.md` | **交接单**：给队员哪些文件/信息 + 给他的 AI 的第一段话 | 发外包前照它走，别手挑文件 |
| `.tools/ctfy-src/` | **平台源码**（契约真源，别去网上找文档） | 见 §6 |
| `PROGRESS.md` | 耐久 checkpoint（§3.1-§3.7 是本轮全部结论） | 先读 §3.3-§3.7 |

## 3. 已完成

- 框架跑通：MCP 握手、多智能体、预算三层刹车、实例生命周期、转录、49 个单测。
- **A0 完成**（题型信息送达 + 提示工具放行）：tags/difficulty/solves_count 入库并送达三处提示（协调者 brief / crew 专家背景 / 单 agent 初始提示）；老 state 自动补线索；`list_hints/view_hint` 真正放行到模型工具面。M1 已在真实平台验证（详见 PROGRESS §3.8）。**关键：`run_role` 原本根本不给专家 Challenge 对象，只改 `_brief` 够不到专家。**
- **A1 完成**（确定性 Ledger）：`ledger.py` 记录归一化 args（URL query 排序、body JSON 键排序），**crew 跨 delegate 共享**；同参数重复调用硬拦，方向证伪只定向提示不硬拦；`_prior_digest` 改为账本为主。真实转录回放：**能拦 18 次重复（≈15.5 万 prompt token）、`POST /api/login` 18 个候选全 401 → 证伪成立（M3）**，详见 PROGRESS §3.9。
- **A2 切片完成**（证据契约）：`report` 必填 `evidence`，必须是某次工具输出的逐字连续子串；无证据却在声称发现 → 拒绝重交（上限 2 次，超限打 `[无逐字证据·未验证]`）；否定型结论不要求证据；证据写进 `ledger.observations` 并以 `[evidence|<来源>]` 回传协调者。踩坑：否定式「未拿到凭据」曾被断言词误伤（已用正则摘除）、转录追加写导致测试计数翻倍（用例开头 unlink）。详见 PROGRESS §3.10。
- **A2 全部完成**（伴生文本 + 派活契约，用户单独批准后实施；48/48 绿，见 PROGRESS §3.11）：新 `companion.py` 把 `PLAN:`/`REFLECTION:` 做成**有状态**的（不是只写进提示词）；`delegate` 改成 `role + objective + done_when`（`task` 仅兼容）。**关键设计：停滞判定的「推进」信号只认账本新增的、非重复的尝试**——模型自述「我推进了」不算数。**尚未真实 run 验证。**
- **全量代码审计 + 修复**（2026-10-25；48/48 绿，详见 `docs/audit-2026-10-25.md`）：三个探针实测坐实 5 个缺陷并全部修复——#1 非法端口让 `normalize_url` 抛 `ValueError` 击穿整个 run（`_ledger_gate`/`_ledger_after` 均在 try 外）→ 归一化降级 + gate/after 包 try；#2 delegate 去重键丢 `objective`（A2 回归，同 role 所有派活撞同一键）→ 归一化改读 objective；#3 `note`/`report` 进账本伪造「推进」信号 → `_ledger_after` 对 `NO_DEDUPE` 跳过；#4 HTTP 缓存跨题不清理 → `solve` 入口 clear；#5 账本摘要双重注入 → 删 `run_role` 前置。补 `case_audit_regression` 反向断言。
- **misc 能力外包全流程**（外包→审查→合并，用户批准后实施；**49/49 绿**，见 PROGRESS §3.12–§3.13 与 `docs/review-misc-module.md`）：3 个纯 stdlib 工具（decode_blob/unpack_archive/hexpeek）+ 3397 字提示词 + 3 道夹具，全部离线验收。**P0 坑：`roles/misc.json` 不会被框架读（框架只读 `config/agent.json` 的 `extra.roles`），必须合并进 config 才生效**；已加 config↔roles 防漂移断言。
- 两轮实测（CTFARENA-035）与 6+4 项修复：https 端口护栏、`_compact_history` 滑窗、grep、角色深合并、预算前置检查、`try/finally` 释放实例、`limit` 参数、`**kwargs` 过滤、delegate 摘要、预算扩容。全部技术生效（详见 PROGRESS §3.1/§3.2）。
- 开源调研（读源码，非二手）：Cybench / HackSynth / PentestGPT 新版，结论见 §5。
- 平台契约逐字读出：rollup / task.json / env / 网关，见 §6。

## 4. CTFARENA-035 的硬结论（**不要再重推**）

**地面真相（黑盒实测）**
- 真实接口只有 3 个：`/api/team`、`/api/company`、`POST /api/login`。
- **所有未知路径都返回同一份 215B `index.html` + 200**（SPA 兜底）——「200」在这题上不代表端点存在。`/.env`、`/robots.txt`、`/api/flag`、`/api/admin`、`/team/*.jpg`、`/assets/*.js.map`、`/graphql`、`/.git/HEAD` 全是 215B。
- flag **只能**来自 `/api/login` 成功响应体（前端 `i.value = await c.json()` 后渲染 `<pre>`）。
- 服务端 `X-Powered-By: Express`；query 参数被忽略；无附件（`attachments: []`）；无提示（`hints.tiers: []`）；图片不存在（无 EXIF 路）。
- **`get_challenge` 原始字段（关键）**：`difficulty=easy`、`solves_count=10`、`tags=["owasp_2025_a07","authentication_failures","weak_password","osint","social_engineering","express","vue3"]`。

**凭据探索已穷尽（~1202 次全 401，别再试）**
216 数据词 + 168 组合（词+年份/人名+宠物名）+ 20 SQLi/NoSQL/类型混淆 + 10 请求体变形 + 488 补漏（漏掉的数据词 golden/retriever/tortoise/endurance…、带空格标点的短语原形、8 种用户名变体含全名/邮箱/拼接）+ 300 经典弱口令表。**密码仍未找到**——但 tags 证明**方向就是弱口令+OSINT+社工**，所以不要断言「凭据路是死路」（这是上一轮的判断错误，已修正）。

**两轮 run 数据**
| | run1（6 修复） | run2（+4 修复） |
|---|---|---|
| 调用/token | 34 / 302,145 | 50 / 504,953 |
| 协调轮/delegate | 5 / 3 | 8 / 6 |
| report（真交结论） | 0 | 1 |
| flag | 0 | 0 |

run2 工具分布：`http_request 65 / delegate 6 / note 1 / report 1` —— **平台工具 0 次**。

**根因链（逐环已验证）**
`Challenge` 数据类没有 tags/difficulty/solves_count → `from_dict`/`normalize_challenge` 丢弃 → `upsert` 只合并 4 字段 → `_brief` 无法展示 → 专家从不调平台工具（协调者又没有平台工具）→ **agent 全程不知道这是道 weak_password/OSINT 题，50 万 token 盲跑**。

## 5. 开源调研结论（该抄什么）

| 项目 | 机制 | 采纳 |
|---|---|---|
| **Cybench** `agent/agent.py`(734行)+`agent/prompt.py` | 强制四段自述：Reflection / **Research Plan and Status（未改则原样复制）** / Thought / **Log（已执行命令+结论枚举）**；子任务脚手架；`iterations_until_hint`；头尾截断；反幻觉剥离 | 借 Plan/Reflection 思想。**Log 由确定性 ledger 取代**（它的 Log 靠模型自觉复制，且会被自家截断丢弃）。**子任务机制不可复制**（依赖基准自带答案） |
| **HackSynth** `pentest_agent.py` | Planner 只看滚动摘要、从不见原始输出；Summarizer 用新摘要整体替换 | 借「滚动摘要」控制上下文；**不借**「看不到原文」（web 题要精确响应体） |
| **PentestGPT 新版** `pentestgpt_agent/plan.py`+`agents.py`+`loop.py` | 计划是**校验过的状态对象**：TaskKind(discover/enumerate/test/exploit/verify/recover) + `done_when` + `basis_ids` 证据引用；一次只租一个任务；`EXPLOIT` 必须先有完成的 `TEST`；无证据不许 finish；Executor 输出 `evidence_excerpt`＝**单一输出里的逐字连续子串** | 全采纳为设计蓝本（简化版） |
| **D-CIPHER**（arXiv 2502.10931，**无公开代码**） | Planner-Executor + 动态反馈 + Auto-prompter | 仅作校准：SOTA 也只有 **22.0% NYU / 22.5% Cybench / 44.0% HTB** → 我们 0/1 不算异常 |
| **EnIGMA**（arXiv 2409.16165，ICML 2025，代码在 **SWE-agent v0.7**） | 交互式 gdb/netcat 工具；命名 **soliloquizing**（模型幻觉观测、不与环境交互） | pwn 轨道时再读；soliloquizing 是 evidence 纪律要防的 |

**方法论坦白**：上述 ★ 评级是机制合理性判断，**没有任何 ablation 数据**支撑某个机制的增益。真正的证据只能来自我们自己的验收指标（M1-M5）。

## 6. 平台契约（从 `.tools/ctfy-src` 逐字读出）

**rollup（`eval/harness.py:125-137`，平台自有 harness 的字段，可照抄）**——写 `$CTFY_WORK_DIR/result.json`；**用户镜像不读 stdout**（`result.stdout_fallback` 为 false）：
```json
{"challenge_id","instance_id","solved","fully_solved","input_tokens","output_tokens","turns","tool_calls_made","stop_reason"}
```
`stop_reason` 取值：`final | max_turns | halted | cut_off | refused | dry_run | done`（**`exit_<n>` 前缀才算 aborted**）。

**task.json（`core/agent_brief.py:399-442`，schema_version=2）**：
```json
{"schema_version":2,"run_mode":"instance|qa|defence|engagement|awd",
 "challenge":{"id","name","description"},"competition_id",
 "questions":[{"id","prompt"}],"gated_questions","scope","rules_of_engagement",
 "deliverable","platform_url","max_turns":null|int,"result":{"path","stdout_fallback"},
 "target_instance":null|str}
```

**环境变量（以 CTFY-0001 §1.1 为准，用户镜像实收）**：`CTFY_PLATFORM_URL`、`CTFY_TOKEN`、`CTFY_EVAL_CHALLENGE_ID`、`CTFY_EVAL_COMPETITION_ID`、`CTFY_EVAL_MAX_TURNS`（**缺失=无预算**）、`CTFY_WORK_DIR`、`CTFY_EVAL_DRY_RUN`（dry run 才为 `1`）。
**⚠️ `CTFY_MCP_URL` / `CTFY_TARGET_URL` 不存在于用户镜像**——它们只出现在平台自有 harness（`eval/external.py`）。MCP 地址只能从 `$CTFY_WORK_DIR/mcp.json` 或 `CTFY_PLATFORM_URL + /api/v1/mcp/` 拼。`config.py:50` 的 `CTFY_MCP_URL` 映射是死项。
**LLM 网关**：`LLM_FLAVOR`（`openai` | `anthropic`）+ `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`；平台同时下发对应 provider 的原生变量名（`OPENAI_*` 或 `ANTHROPIC_*`）。**`LLM_API_KEY` 为空是真实状态，必须大声失败，禁止回退自有 key**；`LLM_BASE_URL` 与 `LLM_API_KEY` 是「一个决定」，有 base url 就必须用。

**result.json（CTFY-0001 §3）**：写 `$CTFY_WORK_DIR/result.json` 后 `exit 0`（非 0 一律判失败）。字段全可选，`schema_version: 2` 只是 advisory；核心字段 `answers`（按 question id，平台代交）、`solved`（**平台按自己的提交记录重算，写它不加分**）、`turns` / `tool_calls_made` / `input_tokens` / `output_tokens` / `stop_reason` / `writeup`（可选 markdown，不参与判分）。dry run 写 `{"schema_version":2,"solved":[],"fully_solved":false,"stop_reason":"dry_run"}` 再 exit 0。

**work dir 里还有 `mcp.json`**（`docker_provider/harness.py:775` `TASK_MCP="mcp.json"`）：平台预写好的 MCP 客户端配置（含端点与鉴权方式），dry run 不写。我们的 `platform.py` 自己从 env 构建、不强制读它，但 B1 必须知道它存在——平台文档明说「`--mcp-config $CTFY_WORK_DIR/mcp.json` is the whole integration」。

## 7. 已批准的方案（按此执行）

### 能力线（本地可测，串行）
- **A0 题型信息送达**（✅ 已完成，48/48 + M1 真实平台验证，见 PROGRESS §3.8）：`state.py` Challenge 加 `tags/difficulty/solves_count` + **扩 `upsert` 合并列表**；`cli.py` normalize 加三字段 + **摘掉 `pick` 里的 "tags" 兜底**；**三处确定性送达** `难度 X | 已解 N 队 | tags: …`：`crew.py:_brief`（协调者）、**`solver.py:_role_context`（`run_role` 专家背景——原方案只写 `_initial_prompt`，那是 `solve()` 专用、够不到 crew 专家，必须保留）**、`solver.py:_initial_prompt`（单 agent 路径）。验收 M1：已通过。
  **附带项（同样 A0 做）**：`PLATFORM_SCOPE["read"]`（crew.py:30-41）补 `list_hints` / `view_hint`——平台允许清单里有这两个工具（`list_hints` 需 `challenge_id`+`competition_id`，返回 `tiers` 字段），我们没放行 → **agent 结构性拿不到提示**。035 的 `tiers=[]` 用不上，但正赛有 hints 的题会吃亏。
- **A1 确定性 Ledger**（1 天）：新文件 `ledger.py`（`Tried`/`Hypothesis`/`Observation`/`Ledger`），**去重键含归一化 args**；挂在 `solver.py:717`（run_role）与 `solver.py:552-560`（单 agent）以及协调者循环；`_prior_digest` 改为渲染 ledger；`rotate(k=3)` 做假设轮换并注入「禁止再试」。验收 M2 跨 delegate 重复=0、M3 ≥1 次 open→refuted。
- **A2 输出契约 + 逐字证据**（✅ 已完成，48/48）：**保留 tool_calls，不照搬自由文本解析**；`report` 增加必填 `evidence`（逐字子串，写入 ledger.observations）；系统提示加 `PLAN:`/`REFLECTION:` 伴随文本（**由 `companion.PlanTrack` 消费**，不是只写进提示词）；协调者派活改成「一个 objective + done_when」。验收 M4 已离线端到端；**M6（计划漂移/停滞）指标已生成进 summary["plan"]，待真实 run 取证**。
- **A3 题型方法卡**（2 小时，**需用户单独批准**——属提示词工程，与「先不管提示词」冲突）：tags → 任务序列/检查项映射。
- **A4 批量基线**（1 天）：10 道 web，**单题预算压到 150-200k**（500k×10 太贵）。产出命中率表 + 失败归因。

### 交付线（B3/B4 需用户提供外部资源）
- **B1 适配层**（1 天）：新 `src/ctfy_agent/agent_main.py` + `python -m ctfy_agent agent`：读 task.json → 映射 env 到 Config → `run_mode=="instance"` 走 Crew、`target_instance` 非空则不重复起实例 → 映射 rollup（含 stop_reason 枚举转换）→ 写 `result.path` **且** print。用 fake MCP + task.json fixture 离线验收。
- **B2 网关流式**（0.5 天）：`llm.py` 改 `"stream": True` + urllib SSE 解析（`data:` 行 → `[DONE]`）。
- **B3 Docker build/push**：**需要用户提供可跑 Docker 的环境**。
- **B4 三道门**（镜像注册 → 组织审批 digest 锁定 → provider key）：**需要用户提供 key**；外部人工延迟大，**应尽早并行启动**。

## 8. 本地环境与操作要点（踩过坑）

- Python：`$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`
- 必须先 `$env:PYTHONIOENCODING="utf-8"`；PowerShell 5.1 **不要**用 `Tee-Object -Encoding` 或 `*>`，用 `| Out-File -FilePath X -Encoding utf8`
- 跑题：`cmd /c run.cmd crew CTFARENA-035 > runs\x.txt 2>&1`，作为**后台 job**；先确认转录文件存在再判定跑过
- 单测：`python tests/smoke.py`（须 **49/49**，当前基线；改动后保持全绿）
- 无 Docker / 无 WSL / 非管理员
- `web_search` **不可用**（搜索后端缺 API key）；`raw.githubusercontent.com` 被 DNS 拦；`api.github.com` 限流；**`git clone` 可用**（用 `--filter=blob:none --no-checkout` 做元数据克隆，再 `git show HEAD:path` 取单文件）；jsDelivr 取单文件可用；arXiv `export.arxiv.org` 可用
- 凭据在 `.env`（`CTFY_TOKEN`、`DEEPSEEK_API_KEY`），**引用时打码**
- 计费/额度：预算按题**累计**（`state.json` 的 `tokens`），重跑要先跑 `runs/preflight_035.py`（清 tokens/status/instance/tool_counts 并轮转转录）
- 实例配额 = 每题 1 个；`start_instance` 必须带 `competition_id`（否则 403 `competition_required`）
- **`runs/` 分析脚本**（全部带 `.txt` 输出）：`preflight_035.py`（**重跑前必跑**：清预算/状态/实例计数 + 轮转转录）、`dump_login.py`（M2 统计登录尝试用）、`verify_6fix.py`、`dump_http.py`、`probe_content.py`、`probe_challenge.py`、`brute_cands.py`、`inject_test.py`、`surface_probe.py`、`login_get_test.py`、`combo_test.py`、`audit_close.py`、`common_pw_test.py`
- **转录结构** `runs/default/transcripts/<ID>.jsonl`：追加式、**跨 run 累积**，按 `## 题目` 分段；每行 `role/step/tool/args/result/at`；**result 截断到 2000 字符**——完整响应体只在模型上下文里，转录拿不回。验尸统计时防「delegate 汇报里引用拦截文本」被重复计数（run2 的『2 次合规拦截』实为 1 真 + 1 引用）
- 竞赛 id：`TPCTF` = `c0d8b4e3dd3b42c095d63d7dfa66431f`，练习 `CTFARENA` = `4394a10aa5d24f97a1ec3301ca718a45`

## 9. 禁忌（别重做）

1. **不要再穷举 035 的凭据**（~1202 次已试，全 401）。
2. **不要断言「凭据路是死路」**——tags 证明方向对，是候选生成覆盖不足。
3. **不要照搬 Cybench 自由文本 COMMAND 格式**（我们用 OpenAI tool_calls，会倒退）。
4. **不要未经批准改提示词内容**（用户明确「先不管提示词」，A3 需单独批准）。
5. **不要用 500k 预算跑批量**（10 题 = 5M token）。
6. **不要重复审计 tags 发现**（PROGRESS §3.4/§3.6 已固化）。

## 10. 下一步

**A0 + A1 + A2 已全部完成**（49/49 绿；M1 真实平台验证、M2/M3 真实转录回放、M4/M6 离线端到端，见 PROGRESS §3.8–§3.11）。A2 的两项提示词改动已获用户单独批准并实施。**另完成一轮全量代码审计并修复 5 个缺陷（见 §3 与 `docs/audit-2026-10-25.md`），并完成 misc 能力外包全流程接入（见 §3 与 `docs/review-misc-module.md`）。**

**近期新产出（新对话必读）**：
- `docs/b1-adapter-plan.md` —— B1 完整方案 + 可行性审核（8 条从平台源码读出的硬事实 + 容器内会踩的 G1-G8）。
- `docs/report-ctfy-rules.md` —— 官网规则调研；标准全文已落地 `runs/site/standards/CTFY-0001.md`（943 行）、`CTFY-0002.md`（1285 行）。
- `docs/HANDOFF-2026-10-25.txt` —— 本文件的纯文本可粘贴版（新对话首条消息直接用）。
- `docs/review-misc-module.md` —— misc 外包交付包验收报告（P0 缺口、核验矩阵、合并步骤）。

剩下待决策的只有 **A3 题型方法卡**（属提示词工程，仍需单独批准）。

**⭐ 唯一硬阻塞**：注册模型时 `LLM_FLAVOR` 是 `openai` 还是 `anthropic`？我们 `llm.py:120` 只有 OpenAI `/chat/completions`；若平台按 anthropic 下发，镜像完全不可用（需另写客户端，+0.5 天）。**这条确认前，B1 的 LLM 接线无法定案**（其余 90% 可先写离线版）。

下一步：
1. **B1 适配层**（关键路径，比赛交的是镜像）：方案已就绪，先用 fake MCP + task.json fixture 做离线版；`agent_main.py` 必须「启动即写 provisional result.json → dry_run 分支 → 非 instance 拒跑 → env 组 Config → 读 mcp.json → Crew（**关掉 stop_instance**）→ 原子写 rollup」。容器内 G1-G8 见方案。
2. **A4 批量基线**（§7）：单题预算压到 150–200k，产出命中率表 + 失败归因。**A0/A1/A2 至今没有真实 run 验证**——A4 就是第一次真机检验，建议先单跑 1 道确认新契约不崩。
3. **出题（authoring）能力**：正赛 `authoring_enabled=true`（11/21 三个 2h 窗口 + 人工审），我们目前**零支持**，是被忽略的一块，需先调研再排期。

⚠️ 赛事硬约束（`GET /api/v1/competitions/c0d8b4e3dd3b42c095d63d7dfa66431f` 实测）：`internet_policy=deny`（容器无外网）、`max_instances_per_team_effective=1`、3 段 session（11/15、11/21、11/22 各 10:00–20:00 CST）、同分先解出者靠前、报名 11-13 20:00 截止（已 177 队 > 150）。规则附件当前为空，判罚细则未公开。

⚠️ 重跑 CTFARENA-035 前必须先 reset（该题累计 504,953 已超 500,000），且一次约 20–50 万 token。
⚠️ A2 新增 `PLAN:`/`REFLECTION:` 会让每步输出多约 30–60 token（估算，未实测）；按 50 步算约 1.5–3k token，相对 8.6k/调用的 prompt 占比很小。
⚠️ **A2 的最大未知**：035 的历史转录里 131 条记录只有 **7 条**有非空 assistant 文本（其余只发 tool_calls、不写文本）。所以模型很可能压根不写 PLAN，`missing_steps` 会接近 100%、2 次提醒配额用完即止——机制安全降级但也失效。首次真实 run 优先看 `summary["plan"]["missing_steps"]` 与 `plan_changes`；缺失率过高说明提示词强度不够，属提示词改动，**要再批准**。

## 11. 与用户的协作偏好（新对话必读）

- **只认实验数据，不猜测**；每个断言先自查——用行号、转录或实测输出验证，不凭印象写数字。
- 方案先批准再实施；**提示词内容类改动必须单独批准**（用户明言「先不管提示词」，A3 就是因这条单列的）。
- 密钥（`.env` 里的 `CTFY_TOKEN` / `DEEPSEEK_API_KEY`）引用时打码。
- 两条线并行：外部资源（Docker、provider key）到位前，先把本地可测的部分做完。
- 验尸/统计时警惕**同一事实的多重计数**（拦截文本被汇报引用、echo 重复），先归类再报数。
