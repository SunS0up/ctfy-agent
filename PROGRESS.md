# TPCTF 2026 智能体赛道 — 工作进度存档

> 最后更新：当前会话。此文件用于跨会话续接，记录已确认事实、已完成工作、待办与阻塞。

## 0. 目标与结论（一句话）

构建 TPCTF 2026 智能体赛道参赛 agent（容器镜像形态，`task.json` → `result.json` 契约）。
**方案已确认可行**，容器契约已从官方 `ctfy` 源码完整拿到（不再靠猜），现有代码 90% 可复用。

---

## 1. 已确认的容器契约（CTFY-0001，schema v2）—— 权威来源：ctfy CLI wheel

`ctfy 0.7.0` 源码已解包到 `.tools/ctfy-src/`（wheel 在 `.tools/ctfy-0.7.0-py3-none-any.whl`）。关键文件：

- `ctfy/core/agent_brief.py` — `task.json` 契约（`BRIEF_SCHEMA_VERSION = 2`）
- `ctfy/eval/harness.py` — 平台 harness 的 env 契约（`CTFY_*` 环境变量）
- `ctfy/eval/llm.py` — LLM 网关客户端（OpenAI / Anthropic 双方言）
- `ctfy/eval/external.py` — 外部 agent 模式（`CTFY_HARNESS_CMD` 等）

### task.json 结构
```json
{
  "schema_version": 2,
  "run_mode": "instance | qa | defence | engagement | awd",
  "challenge": {"id": "...", "name": "...", "description": "..."},
  "competition_id": "...",
  "questions": [{"id": "...", "prompt": "..."}],
  "gated_questions": 0,
  "scope": [...], "rules_of_engagement": [...],
  "deliverable": "answers | patch | ...",
  "platform_url": "...",
  "max_turns": 64,
  "result": {"path": "/ctfy/work/result.json", "stdout_fallback": false},
  "target_instance": null,
  "dry_run": false,
  "limits": {"pids": 32, "memory": "8g", "cpus": "2.0"}
}
```
- 平台把 `task.json` + 渲染好的 `README.md` 写进只读的 `/ctfy/work/`（即 `$CTFY_WORK_DIR`）。
- `target_instance` 为空 = 自己 `start_instance`；非空 = 平台已起好 estate（engagement/awd）。

### result.json
汇总对象 `{challenge_id, instance_id, solved, fully_solved, input_tokens, output_tokens, turns, tool_calls_made, stop_reason}`，**同时打到 stdout**（stdout 是 fallback）。真正的计分靠运行中调 MCP `submit_answer`，result.json 只是回执。

### 环境变量（平台注入）
| 变量 | 含义 |
|---|---|
| `CTFY_PLATFORM_URL` / `CTFY_TOKEN` | 平台地址 + scoped bearer（**同时是 MCP 凭据**） |
| `CTFY_EVAL_CHALLENGE_ID` / `_COMPETITION_ID` / `_MAX_TURNS` / `_KEEP_INSTANCE` | 本次任务参数 |
| `LLM_FLAVOR` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` | **模型走平台网关**，`openai` 或 `anthropic` 双方言 |

### 关键坑
- 网关在 Cloudflare 后，**120 秒完整响应上限**；官方 harness 全用流式。我们的阻塞式 LLM 客户端将来需改流式（或超时重试）。
- 官方 harness 的 `result_payload` 见 `harness.py:125`；`emit_rollup` 见 `harness.py:149`（写文件 + print 双通道）。

---

## 2. 赛道事实（此前已确认，勿再猜）

- 赛道类别仅 **在线交互式：Web、Pwn、AI 安全**。Crypto/Reverse/Misc 是「离线附件类」，不在 agent 赛道。
- 报名 2026-10-15 起；**入围测试 11-14~15，初选赛 11-15 10:00–20:00**；正式 11-21~22。agent 赛道上限 150 队。
- 提交物 = **容器镜像**；三道门：①镜像注册（push 平台仓库或引用已发布镜像）②组织审核（异步）③provider 密钥（方言必须匹配，平台网关路由模型请求）。
- 真实 MCP：`https://ctfy.tsinghua.edu.cn/api/v1/mcp/`，server `ctfy 3.4.6`，97 tools，allowlist 26（A0 起含 `list_hints/view_hint`）。
- 练习套：`CTFARENA` id `4394a10aa5d24f97a1ec3301ca718a45`（115 题全 web，easy 7/medium 48/hard 60，仅 50 可见）。
- 团队实例配额 `max_instances_per_team_effective = 1`（单实例槽位，卡实例会堵下一题）。

---

## 3. web 基线：CTFARENA-035（StartupDeck，easy）实测 3 轮

| run | 结果 | prompt tokens | 调用数 | 卡点 |
|---|---|---|---|---|
| 1 | budget_exhausted | 304,984 | 40 | 护栏 bug：https:443 隐式端口被自己拦死 |
| 2 | budget_exhausted | 300,116 | 33 | 上下文爆炸（输入:输出 ≈ 100:1） |
| 3 | budget_exhausted | 295,750 | 36 | 上下文膨胀 + Windows shell 无 grep/ls，被迫按 8KB 逐段读大 JS 包 |

> 复核更正（尸检）：run 2 的 300,116 是**总量**（prompt+completion），prompt 未单独记录；run 1/3 的数是 prompt。另注意：4 次 crew.solve 里夹了 1 次「没重置预算就重跑」的 0 调用秒败，真实 run 是 3 次。

**3 个 bug 已全部修复并测试通过：**

1. **护栏 https:443**（`tools_local.py::_check_target`）：URL 没写端口时按协议默认端口对齐（https=443, http=80），异协议仍拦。
2. **上下文滑窗**（`solver.py::_compact_history` + `_est_tokens`）：16k token 窗口，保留 system+初始 user，整块丢弃最旧步骤，不拆散 tool 配对。
3. **大响应 grep**（`tools_local.py::http_request` + `_render_http`）：`grep` 参数先读全文（上限 2MB）再只回匹配片段 ±80 字符（最多 150 条），专治压缩 JS 单行包。

⚠️ 行为点：**单题 token 预算是跨 run 累计的**（`Budget.load(challenge.tokens)`），烧穿上限后无法重试，需手动重置（见下方命令）。

### 3.1 第三轮实测（6 项修复后，1 次 crew.solve，302,145 tokens / 34 调用）

结果：仍未解出（0 flag / 0 report / 3 个 delegate 全部 step_limit），但**修复效果可量化**：

| 指标 | 修复前（3 次真实 run） | 修复后（本次） |
|---|---|---|
| 416 | 17 | **0** |
| 合规拦截 | 17 | **0** |
| shell 调用（含无效） | 16（全灭） | **0** |
| grep 使用 | 0（功能不存在） | **15** |
| offset 使用 | 无 | **9** |
| delegate 空报告 | 7/15 | **0/3**（都带「最近动作」） |
| JS 包尾部可读 | 不可达（8KB 截断） | **读到 byte 61700–64102** |

**关键突破**：包共 64,102 字节，模型读到了尾部真实代码——
`K("a",{href:"#",onClick:d[1]||(d[1]=bs(a=>t.value="admin",["prevent"]))},"Admin")`
即 **Admin 只是前端改状态，鉴权在前端**（本题题眼），且已取到 `/api/team`(1388B)、`/api/company`(325B)。
旧版 8KB 截断永远到不了 61700，这一条直接证明修复有效。

**新暴露的问题（按浪费排序）**：
1. `http_request(limit=...)` → `TypeError: unexpected keyword argument 'limit'` × **4**（模型幻觉参数直接崩步，浪费 4/30 专家步）
2. `/api/login` **盲猜 20 次** 401（占 33% 调用）：admin/admin、daniel/biscuit、mira/toast… 全猜；已有前端绕过的证据却没走那条路
3. 3 个 delegate 全部卡在 **step_limit=10**，0 次 report（靠 Fix 2 的合成报告兜底）
4. 协调者 **3 轮派活是同一份侦察的重写**（"Investigate…"→"perform the following…"→"thorough recon…"），没基于上轮结论 → 烧掉 2/3 预算
5. 300k 预算 ≈ 34 次调用（prompt 约 8.6k/调用），5 轮协调即耗尽

### 3.2 第四轮修复（代码/配置，提示词类按指示暂缓，本轮实现）

39/39 测试通过、compileall 干净：

1. **`limit` 参数真支持**：`http_request`/`_render_http` 新增 `limit`（clamp 200..5000），模型主动要更小窗口时生效。上次 4 次 `TypeError: unexpected keyword argument 'limit'` 从此消失。
2. **未知参数过滤 + 友好报错**：`LocalTools.call` 改按 `inspect.signature` 分发——未知 kwarg 丢弃并回显「已忽略 + 合法参数清单」；缺必需参数转带清单的 `ToolError`，不再裸 `[执行异常]`（`shell` 未开启的专属提示保留）。
3. **跨轮结论摘要**：`crew._prior_digest` + `_delegate(..., prior=reports)` + `solve` 累积 `reports`——下一轮派活的 task 自动附上前 3 轮专家结论（各 ≤600 字，含「已失败尝试勿重复」锚点）。堵住「跨代重复侦察/重复凭据」的结构性缺口（专家上下文里没有读历史笔记的工具）。
4. **预算上调（一次性实验）**：`max_tokens_per_challenge` 300000→500000，`max_llm_calls` 60→80，为「能否解出」拿能力判定。
5. **暂缓**：web 提示词「前端鉴权三型判别」（内容型、风险高）；web `max_steps` 10→14 + 最后一步 report 提醒（后者属提示词，二者一起再测）。

测试新增 2 例：`case_limit_and_kwargs`、`case_prior_digest`。

### 3.3 第五轮实测（4 修复后）+ 本题真实攻击面（黑盒实测）

**run 2**：50 次调用 / 504,953 token / 8 轮协调 / 6 个 delegate / 65 次 http / 1 次 report / **0 flag**。
修复全部技术生效：`limit` 崩溃 4→0；摘要端到端注入（每个 task 尾部都带）；预算 34→50 次调用。
**但摘要对「防重复凭据」是空的**：Fix 2 的动作清单只记工具**结果**（全是 `401 invalid credentials`），不记**参数**（试了哪组凭据），所以「已失败尝试勿重复」没有内容可依——delegate 3 仍原样重试了 delegate 2 的 admin/admin、daniel/biscuit、daniel/Biscuit。

**黑盒实测得到的本题真相**（起实例直连，不发 LLM）：
- 真实接口只有 3 个：`/api/team`、`/api/company`、`/api/login`；**没有 `/api/flag`、`/api/admin`**。
- **所有未知路径都返回同一份 215B 的 `index.html` + 200**（SPA 兜底）——「200」在本题不代表端点存在。`/.env`、`/robots.txt`、`/api/flag`、`/team/daniel.jpg` 全是 215B，图片根本不存在（无 EXIF 路）。
- 服务端 `X-Powered-By: Express`；`OPTIONS /api/login` 允许 `POST,GET,HEAD`，但 `GET` 也只是 SPA 兜底。
- flag 只能来自 `/api/login` 成功响应（前端 `i.value = await c.json()` 后渲染成 `<pre>`）。
- **凭据不可猜**：216 个单词候选 + 168 个组合候选 + 21 个 SQLi/类型混淆 + 7 个请求体变形 = **412 次探测全部 401**。

**结论（修正我上一轮的判断）**：瓶颈已不在工具链。两轮 ~80 万 token 都烧在同一个假设（猜凭据 → 登录）上，而该假设被 412 次探测证伪。
- 「把 args 写进摘要」仍是个通用正确的小修，但**对本题已经无效**（猜的凭据全是错的，少重复也拿不到 flag）——上一轮把它排为「唯一明确下一步」是过高估计。
- 真正缺的是**策略广度**：模型从不系统枚举攻击面（两轮只碰首页+JS+3 个接口+几个猜测路径），也从不换假设。

### 3.4 第六轮（审计「上轮结论对吗」）——结论修正 + 新根因链

**黑盒累计 ~1202 次尝试全部 401**：216 数据词 + 168 组合 + 20 注入 + 10 请求体变形 + 488 补漏词/短语/8 种用户名变体 + 300 经典弱口令表。补充探测：`/assets/*.js.map` 不存在、36+ 未知路径全是 215B SPA 兜底、query 参数被忽略、`attachments=[]`、提示层 `tiers=[]`。

**决定性新证据（`get_challenge` 原始字段，此前从未读过）**：
`difficulty=easy`、`solves_count=10`、**`tags=[owasp_2025_a07, authentication_failures, weak_password, osint, social_engineering, express, vue3]`**

**修正上轮两处错误**：
1. 「模型押错假设（凭据路是死路）」——**错**。题型就是弱口令+OSINT+社工，模型方向本来是对的；错的是候选空间覆盖（我和它共 ~1200 次都没生成出正确密码）。
2. 「args 写进摘要对本题无效」——随 1 失效：凭据搜索题里去重=同预算更多唯一尝试。

**根因链（本次审计的新发现）**：
`Challenge` dataclass 没有 tags/difficulty 字段 → `normalize` 在入库时丢弃 → `_brief` 不含 → run 2 的 web 专家 **65 次 http_request、0 次平台调用**（协调者又没有平台工具）→ **agent 全程不知道这是道 weak_password/OSINT 题**。平台免费给的题型信息被我们自己扔掉了。

**修复优先级（更新）**：
1. tags/difficulty/solves_count 入库 + 进 `_brief`（确定性信息送达，直接命中本题根因）
2. args 写进 delegate 摘要（恢复相关性）
3. 批量基线（不变）
4. recon 工具（降级：本题枚举已完整，证据不足）

### 3.5 开源 CTF/安全 agent 调研（已读源码，非二手转述）

用 `git clone --filter=blob:none --no-checkout` 元数据克隆后按需取 blob（api.github.com 限流、raw.githubusercontent 被 DNS 拦、jsDelivr 因仓库 >50MB 拒绝目录树，git 是唯一通路）。

**① Cybench（andyzorigin/cybench，40 道 CTF）** — `agent/agent.py`(734 行) + `agent/prompt.py`
- 单链迭代：LLM 输出 → 解析 `COMMAND:` 或 `ANSWER:` → bash 执行 → 回注 `Observation: ... This is the end of assistant message`。
- **强制自述四段**：`Reflection` / `Research Plan and Status`（完整计划，未改则原样复制）/ `Thought` / **`Log`（已执行命令枚举 + 每条结论，明确要求复制上轮列表再追加，防止重跑）**。
- 子任务脚手架：失败后直接告知该子任务答案再进入下一个；`iterations_until_hint` 到点注入 hints。
- 截断：保留最近 N 条 response / N 条 observation + token 级头尾各半截断。
- 反幻觉：剥离伪造的 `----------Message from assistant/system----------`。

**② HackSynth（aielte-research/HackSynth）** — `pentest_agent.py`
- 双角色：**Planner 只看 `summarized_history` + 目标文本，从不看原始输出**；**Summarizer 收到 `summarized_history + new_observation`，输出新摘要整体替换**。
- 观测截断 2000 字符；`<CMD>...</CMD>` 解析后 shlex.quote 执行。

**③ PentestGPT 新版（GreyDGL/PentestGPT，`pentestgpt_agent/`）** — `loop.py`/`plan.py`/`memory.py`/`agents.py`
- **计划是状态不是提示**：Supervisor 决策 → `compile_plan()` 校验 → `ValidPlan`；一次只租一个任务（`TaskLease`），`max_decisions=20`。
- `TaskKind` 方法论分类：**discover / enumerate / test / exploit / verify / recover**；`done_when` 必填完成判据。
- **`basis_ids` 证据引用**：任务必须引用既有 observation；`EXPLOIT` 必须 cite 并 depend 于同 target 已完成的 `TEST`；`finish` 必须 cite 已完成任务的 observation；依赖必须无环。
- `TEST` 的 objective/done_when 含 "read the flag / gain a shell / exfiltrat" 等短语直接判非法（方法边界硬约束）。
- Executor 输出 `evidence_excerpt`：**必须是单一成功输出里的逐字连续子串**，直接成为 canonical memory；有 flag 的 receipt 立刻结束任务。
- 状态注入 Supervisor 的是紧凑 JSON：tasks / task_history(计数+最近) / observations(id,task_id,evidence) / recent_diagnostics / validation_feedback。

**映射到我们的三个缺口**：
| 我们的缺口 | 对应机制 | 来源 |
|---|---|---|
| 不知道题型（tags 被丢） | authorative state JSON + 题型方法卡 | PentestGPT `_supervisor_prompt` + 平台 tags |
| 无假设管理、重复尝试 | 四段自述里的 Plan/Log；确定性 ledger（含 args） | Cybench `Log` + 我们自己的 fix-2 缺陷修正 |
| 自由文本综合丢证据 | `evidence_excerpt` 逐字引用 + 无条件结束任务 | PentestGPT Executor |
| 一长串不可能指令 | 一次一个任务 + `done_when` | PentestGPT one-task lease |

**不采纳**：PentestGPT 的 trace/episode/lease 修订与崩溃恢复（单进程单次容器运行，过重）；HackSynth「Planner 完全看不到原始输出」（web 题需要精确响应体，只借滚动摘要作为 ledger 压缩）；Cybench 的 bash-only 工具面（我们有结构化 http 工具，更好）。

### 3.6 方案自查（「审查结论是否正确」）

**研究结论修正**：
- Cybench「四段式」实为 4 自述段 + Command/Answer 动作行；其 Log 靠模型自觉复制且会被自家截断丢弃 → 确定性 ledger 严格优于（强化 ②）。Cybench 最强杠杆（基准自带答案的子任务 + 失败即告知答案 + hints）**不可复制**（生产无 ground truth）。
- ★★★ 评级是机制合理性判断，**无 ablation 数据**；真正证据只能来自我们自己的 M1–M4 验收。
- 新增定位：**D-CIPHER**（arXiv 2502.10931，NYU，无代码）：Planner-Executor + 动态反馈 + Auto-prompter；SOTA 成绩 22.0% NYU / 22.5% Cybench / 44.0% HTB —— 同类架构天花板约 20–45%，我们 0/1 不算异常。**EnIGMA**（arXiv 2409.16165，ICML 2025，代码在 SWE-agent v0.7）：交互式 gdb/netcat 工具（pwn 轨道关键，暂缓）；命名 **soliloquizing**（模型幻觉观测不与环境交互）——evidence-excerpt 纪律即对策。CAI、InterCode 未读（低相关，记录在案）。

**① 实施面已全部核实**（本轮新验证）：
- `normalize_challenge` 只映射 5 字段（cli.py:65-79）；`get_challenge`（cli.py:352）与 `list_challenges`（cli.py:296，实测 50 条）**都带 tags/difficulty/solves_count** → ① 不需要任何额外平台调用。
- `Challenge.from_dict` 过滤未知键 → 老 state.json 向后兼容。
- **坑 1**：`upsert` 只合并 name/category/points/description（state.py:161）——加字段必须同步扩此列表。
- **坑 2**：`pick("category",...,"tags")`（cli.py:76）——category 缺失时会把 tags 列表当 category；加真字段时必须把 "tags" 从 category 回退链摘掉。
- **坑 3**：`PLATFORM_SCOPE["read"]`（crew.py:30-41）缺 `list_hints` / `view_hint`——平台允许清单里有这两个工具（`list_hints` 需 `challenge_id`+`competition_id`，返回 `tiers` 字段，实测 035 为空）→ **agent 结构性拿不到提示**；并入 A0 一起修。035 `tiers=[]` 不受影响，正赛有 hints 的题会吃亏。

**方案修正**：
- **③ 有架构错配**：Cybench 四段式假设自由文本 COMMAND 解析，我们用 OpenAI tool_calls（cli.py:364 已证）。修正版 ③'：LOG→确定性 ledger；REFLECTION/PLAN→工具调用轮的伴随文本；EVIDENCE→从 assistant 文本解析（text 与 tool_calls 共存，可行）。
- **④ 与既定「先不管提示词」冲突** → 拆出单独审批（D-CIPHER Auto-prompter 是已发表同类）。
- **M4 重定义**：凡声称发现/结论的 report 必须带逐字证据（失败题无证据属正常）。
- **战略遗漏**：方案未与交付线（容器适配层 task.json→result.json、流式、Docker build+push、三道门）排期对齐；比赛提交物是镜像，适配层是关键路径。
- **M5 成本**：10 题 × 500k = 5M token、实例配额 1 只能串行 → 批量基线单题预算压到 150–200k。

### 3.7 详细方案（时间充足 → 两条线并行）

**交付线契约已从 `.tools/ctfy-src` 逐字读出**（不再靠转述）：
- rollup 字段 = `harness.py:125-137`：`{challenge_id, instance_id, solved, fully_solved, input_tokens, output_tokens, turns, tool_calls_made, stop_reason}`；写 `$CTFY_WORK_DIR/result.json` **且** print 到 stdout（`emit_rollup`）。
- task.json 字段 = `agent_brief.py:399-442`：`schema_version:2 / run_mode / challenge{id,name,description} / competition_id / questions[{id,prompt}] / scope / platform_url / max_turns(null|int) / result{path,stdout_fallback} / target_instance(null|str)`。
- env 契约（`eval/external.py`）：`CTFY_PLATFORM_URL / CTFY_TOKEN / CTFY_EVAL_CHALLENGE_ID / CTFY_EVAL_COMPETITION_ID / CTFY_EVAL_MAX_TURNS / CTFY_WORK_DIR / CTFY_TARGET_URL`；LLM 网关用 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`（OpenAI 兼容）。
- 我们的 `llm.py:114` 是 `"stream": False` —— 网关要求流式（120s 首字节限制），需自实现 urllib SSE 流式解析。

**能力线实现锚点（本轮已读）**：`state.py:161` upsert 只合并 4 字段；`cli.py:76` `pick("category",…,"tags")` 兜底 bug；`solver.py:717` actions 记结果不记 args；`crew.py:466-479` `_brief` 无 tags；`cli.py:352` get_challenge 与 `cli.py:296` list_challenges 都带 tags。

**顺序**：能力线 ①→②→③'→④→M5 串行本地可测；交付线 B1(适配层)→B2(流式)→B3(Docker)→B4(三道门)，B3/B4 需用户提供 Docker 环境 + provider key（可与我做 A 并行准备）。

### 3.8 A0 完成（题型信息送达）+ 两个计划外发现

**测试 41/41 全绿**（原 39 + `case_challenge_meta` / `case_hint_scope`），`compileall` 干净。

改动文件：
| 文件 | 改动 |
|---|---|
| `state.py` | `Challenge` 加 `tags/difficulty/solves_count`；`upsert` 合并 tags 并集 + 难度/已解；新增 `normalize_tags/normalize_difficulty/normalize_solves`（宽容解析）+ `Challenge.facets()` 单行文本；`from_dict` 归一 |
| `cli.py` | `normalize_challenge` 加三字段；**摘掉 `pick("category",…,"tags")` 兜底**；新增 `ensure_challenge_meta()` 并替换 cmd_solve/cmd_crew 的重复块 |
| `crew.py` | `_brief` 输出 facets；`PLATFORM_SCOPE["read"]` 加 `list_hints/view_hint` |
| `solver.py` | `_initial_prompt` 输出 facets；**新增 `_role_context`**（见发现 ②）；`view_hint` 已放行 → 两处过时注释更正 |
| `platform.py` | `MODEL_TOOLS` 加 `list_hints/view_hint`（见发现 ①） |
| `docs/verification.md` | 工具面 24→26；`view_hint` 从「风险表」移入「已放行+护栏」段 |
| `tests/smoke.py` / `fake_mcp_server.py` | +2 用例；fake 的 `web-01` 复刻真实题型字段形状（`solves_count`/`difficulty`/`tags`） |

**发现 ①（附带项的隐藏必要条件）**：只改 `PLATFORM_SCOPE` 不够。`solver._platform_names()`（单 agent 路径）取的是 `platform.model_tools()`，而它按 **`MODEL_TOOLS`** 过滤（`platform.py:145-150`）；`PLATFORM_SCOPE` 只作用于 crew 的角色分组（`crew.py:174-180`，取 `platform.tool_names()` 全集）。**两处都加才真正放行**。`list_hints` 的 `challenge_id`+`competition_id` 由 `solver._autofill` 自动补齐，模型无需自己拼（已在测试里固化）。

**发现 ②（A0 原本够不到最需要它的人）**：`run_role` 只把 `[system, task]` 交给专家（`solver.py` 原 636-639），**专家压根收不到 Challenge 对象**——所以只改 `_brief`/`_initial_prompt` 时，crew 路径下的 web 专家仍然不知道题型（而 crew 就是我们实际跑的那条路）。新增 `Solver._role_context(challenge)`，把 id/分类/**难度/已解/tags**+题面摘要作为一个数据块前置到 task 前。
> 判定说明：这严格说是「往提示里塞内容」，但它只是平台元数据（无新指令、无方法卡，不触及 A3 边界）。**若判定越界，回退一行即可**（`run_role` 里那个 `context` 前置），代价是 crew 专家退回「只能靠协调者转述题型」。

**老 state 的坑（实测确认）**：`cmd_crew/cmd_solve` 原本只在「库里没有这道题」时才 `get_challenge` → `runs/default/state.json` 里的 035 永远补不上 tags。`ensure_challenge_meta` 对「已在库但缺线索」也补一次（`get_challenge` 免费，不耗 token/实例），补到线索后不再重复打平台。

**M1 真实平台验证**（`runs/verify_a0.py`，只握手 + get_challenge，零 token、不起实例；输出 `runs/a0_verify.txt`）：
```
[before] tags=[] difficulty='' solves=None      ← 根因现场：旧 state 确实丢了
[题型] CTFARENA-035 难度 easy | 已解 10 队 | tags: owasp_2025_a07, authentication_failures,
       weak_password, osint, social_engineering, express, vue3
[after ] tags=[...weak_password...] difficulty='easy' solves_count=10
M1: brief 含 weak_password ✅ / 专家背景含 weak_password ✅ / 难度+已解 ✅
```

**未做（需决策）**：真实 `crew CTFARENA-035` 复跑——该题累计预算 504,953 已超 500,000，重跑必须先跑 reset（PROGRESS §6），且一次约 20–50 万 token。

### 3.9 A1 完成（确定性 Ledger）+ 用真实转录回放校准参数

**测试 43/43 全绿**，`compileall` 干净。新文件 `src/ctfy_agent/ledger.py`（约 400 行，纯 stdlib）。

**结构**
| 对象 | 内容 |
|---|---|
| `Tried` | 一次已执行调用：归一化去重键、人读 label（**含凭据/路径**）、结果 digest、role、repeats |
| `Hypothesis` | 策略簇状态机（`http POST /api/login`）open → refuted，含 attempts/failures/successes/last_failure |
| `Observation` | 逐字证据（flag 形态自动收录；A2 的 `report.evidence` 将写进同一处） |

**去重键包含归一化 args**（上一轮根因就是只记结果不记参数）：
- URL：主机小写、去默认端口、**query 排序**；body：JSON **按键排序**（`{u,p}` 与 `{p,u}` 同键）；
- `grep/offset/limit` 是投影参数，**不同投影算不同尝试**（同投影重复读才算重复）；
- 写死丢弃 `headers/timeout/verify`（噪声，不影响结果）。

**三条循环接线**
- `Solver._ledger_gate()` 执行**前**拦「同 tool + 同归一化 args」的重复（`note/report/submit` 永不拦）；`_ledger_after()` 执行后记账，并在**新证伪**时返回 `rotate()` 文本注入。
- 单 agent `solve()`：自建一本账，loop 内接 gate/after，summary 增加 `ledger` 统计。
- `run_role(..., ledger=)`：**crew 跨 delegate 共享同一本账**，每个专家开局收到账本摘要（含别人试过的参数）。
- 协调者循环：`delegate` 的 role+task 也进账（同任务重派会附 `[ledger]` 警告）；账本快照落转录（`{"role":"ledger","stats":…,"render":…}`）。
- `_prior_digest(prior, ledger=)` 改为「账本为主 + 上轮结论文本为辅」（保留原 `prior` 行为，老测试不受影响）。

**设计边界（有意为之）**：只**硬拦**完全相同的调用；簇被证伪只做定向提示、不硬拦——tags 证明方向可能对（`weak_password`），硬拦会把对的方向一起杀掉。

**用真实转录回放校准**（`runs/ledger_replay.py`，零成本，结果 `runs/ledger_replay.txt`）——run 2 的 131 条记录、72 次工具调用：

| 指标 | 值 |
|---|---|
| 不同调用 | 54 |
| **会拦下的完全相同调用** | **18**（≈ 省 154,800 prompt token，按 §3.1 实测 8.6k/调用估算） |
| 重复最多的键 | 同一 JS 包同一 grep+offset **×4**；`GET /` **×3**；`/api/team`、`/api/company` 各 ×2；admin/admin、daniel/biscuit… 各有重复 |
| 被证伪方向 | **1 个：`http POST /api/login`（18 个不同候选全 401）** → M3 在真实数据上成立 |

**回放暴露并修掉的两个真问题**：
1. `refute_after=20` 永远不会触发——035 一次 run 内同一方向只有 **18** 个不同候选。默认改 **12**（只提示不硬拦，风险可控）。
2. `looks_failed` 按纯文本匹配把「200 + 大 JS 包」误判成失败（4 次，因为包里有 `error` 字样）。改为 **`http_request` 优先解析结构化 `status`**，并对被截断的 JSON 用正则兜底（`"status":\d{3}`）。

### 3.10 A2 切片完成（证据契约，**不含 PLAN:/REFLECTION:**）

用户决定 A2 先只做证据契约，提示词部分（`PLAN:`/`REFLECTION:` 伴随文本）按 §11 另行批准。**测试 44/44 全绿**（连跑两遍验证幂等）。

**契约**
- `REPORT_SPEC` 增加 `evidence` 参数并标 `required`（刻意不断言 `text` 里的 flag，工具 schema 只描述**要求**，不做语义校验）。
- `Solver._check_report(ledger, text, evidence, rejections)` 三个判定：
  1. 给了 evidence 但**不是任何一次工具输出的逐字连续子串** → 拒绝（改写/概括/翻译都会被抓）；
  2. 没给 evidence 却在**声称有发现** → 拒绝，并给出「要么补证据、要么改成否定型结论」两条路；
  3. 否定型结论（「该方向 18 次均 401，未拿到凭据」）**不要求证据**（失败题属正常）。
- 证据命中：写入 `ledger.observations`（带来源 label），并把 `[evidence|<来源>]` 追加进报告 → **协调者也看得到出处**（M4 可审计）。
- **拒绝有界**：`MAX_REPORT_REJECTIONS=2`，超限后放行但给结论打 `[无逐字证据·未验证]` 前缀——不给模型「靠反复被拒烧步数」的死循环。

**Ledger 侧新增**：`Tried.raw`（原始输出，`RAW_CAP=4000`，只保留最近 `raw_window=60` 条）、`verify_evidence()`（逐字匹配 + **空白折叠兜底**：模型复制大段 JSON 常改缩进）、`recent_sources()`（被拒时回显真实片段供原样复制）、`claims_discovery()`。

**踩到的坑（都有实测）**：
1. **否定式误伤**：`CLAIM_MARKERS` 里的「拿到」把「**未**拿到凭据」判成了声称有发现 → 检测前先用正则摘掉否定式前缀（`未/没/没有/not/failed to…`）。
2. **转录是追加写**：测试断言「被拒 2 次」在第二次跑套件时变成 4 次 → 用例开头 `transcript.unlink(missing_ok=True)`。两次连跑均 44/44。
3. 原有 `case_crew` 的「专家声称找到 flag 但不带证据」被新契约拦下（**预期行为**）→ 用例改成「先 get_challenge、再带逐字证据 report」，顺带覆盖 `[evidence|…]` 注入。

**A2 的另一半（`PLAN:`/`REFLECTION:` 伴生文本、派活改 objective+done_when）已由用户在 §3.11 单独批准并落地**，本节只记录证据契约部分。

### 3.11 A2 全部完成（伴生文本 + 派活契约）——用户单独批准后实施

**测试 46/46 全绿**（连跑两遍幂等），`compileall` 干净。新增 `src/ctfy_agent/companion.py`（约 160 行，纯 stdlib）。

**为什么 PLAN 不能只是提示词里的一句话**：Cybench 的 "Research Plan and Status" 靠模型自觉原样复制来防漂移，没有任何代码侧消费者；模型改写措辞或自家截断丢掉，机制就失效。这里让 PLAN 有状态：

| 对象 | 内容 |
|---|---|
| `parse_companion()` | 纯函数；宽容解析 `**PLAN:**` / `PLAN：`（全角）/ `1. PLAN:` / `- 计划：` / 多行值；取不到返回空串，**绝不抛异常**（每步都跑） |
| `PlanTrack.observe(content, progressed=)` | `progressed` **只认确定性信号**：本轮账本新增尝试数 > 0（模型自述「我推进了」不算，这正是 soliloquizing 要防的） |
| `reminder()` | 缺标签时的格式提醒，**每 run 最多 2 次**（配额用完闭嘴，不为凑格式烧步数） |
| `stalled()` / `stall_hint()` | 同一 PLAN 连续 ≥3 步且无新尝试 → 提示「换假设或收口」；**每个停滞 episode 只提示一次**（有新尝试后重新武装） |

**接线三处**（都是 `attempts_before = len(ledger.order)`，执行完比对）：`solver.solve`（单 agent）、`Solver.run_role`（专家）、`Crew.solve`（协调者——它的「推进」= 账本里出现新的派活 objective）。PLAN/REFLECTION 同时写进每步 assistant 转录的独立字段，专家回合末尾再落一条 `plan_stats`，M6 取证不用再解析文本。

**派活契约**：`delegate` 从 `role + task` 改为 `role + objective + done_when`（`task` 保留为旧格式兼容，缺 `objective` 时回退）。任务头由 `_delegate_task()` 拼成 `【本轮目标】/【完成判据】`，并要求「满足判据就立刻 report，做不到也 report 说清卡点」——run2 三个 delegate 全是 step_limit、0 report，缺的就是「查到哪算完」。

**去重的刻意选择**：派活去重键是 `role + objective`，**不含 done_when**。只改完成判据的措辞不算新工作，否则模型能靠改写 done_when 绕过去重。

**踩到的坑**：`observe()` 只在「本步有工具调用」后执行，而循环里 `if not calls: break` 在此之前——所以「计划不变但无调用」不会累计停滞。写用例时正好踩中（脚本 4 步只到 stall=2），补一步才触发。这不是 bug，是「停滞」只该在有动作时判定；但**测试脚本必须给足带调用的步数**。

**真实转录上的自检（重要，且是负面证据）**：拿 `runs/default/transcripts/CTFARENA-035.jsonl` 过一遍新解析器——131 条记录里**只有 7 条有非空 assistant 文本**（web 6 + coordinator 1），其余全是「只发 tool_calls、完全不写文本」。这有两层含义：
1. 解析器在 7 条真实样本上**误判 0 条**（旧 run 从未被要求写 PLAN，所以正确结果是空）——但样本只有 7 条，证据很弱；
2. **真正的风险在这里**：模型历史上几乎不产出文本，`PLAN:`/`REFLECTION:` 很可能大面积缺失。届时 `missing_steps` 会接近 100%、提醒配额 2 次用完即止——机制会「安全降级」而不是崩溃，但也等于没起作用。**这是首次真实 run 必须先看的第一个数**；若缺失率过高，说明提示词强度不够，需再批准一次提示词调整（或改成从 tool_calls 前的 content 强制前置标签的格式约束）。

**未做**：真实 run 验证。A0/A1/A2 三项机制至今全部只有离线证据。

### 3.12 misc 能力外包准备（自检 → 修框架缺陷 → kit → 契约测试）

**基线：36 个 case / 48 条 PASS，全绿**；`compileall` 干净。起因是问「misc 能不能外包」，结论是**能，但 v1 工单会坑到接单方**，于是把三件事做成了可执行资产。

**1) 框架缺陷（你批准后修的）**：`_role_system` 只给自定义提示词补 `COMPANION_RULE`/`HANDOFF_RULE`，**不补 evidence 纪律** → 外包回来的 misc 专家每条「声称发现」的 report 都会被 `_check_report` 打回 2 次再打 `[无逐字证据·未验证]`。现在加 `EVIDENCE_RULE`，条件 `"evidence" not in text.lower()`，内置角色分支也追加。
⚠️ 坑：`EVIDENCE_RULE` 正文里**不能出现 "report"**——`_role_system` 用字符串包含判断要不要补 `HANDOFF_RULE`，混进去会让 `case_role_extension` 的 `count("report") == 1` 失真。

**2) `case_misc_contract`（有产物强校验，无产物打印「跳过」）**：roles 片段能被 Crew 吃下（角色可见 / delegate enum / 工具面 ⊆ 平台白名单 / 专家无提交权）、`allow_shell=true`、提示词含 report+evidence、新工具**「SPEC + specs() 注册 + 同名方法」三件齐**、参数名 ⊆ SPEC、输出 ≤6000、未知参数不崩、新工具进账本、夹具 `solve.py` 复现 `expected.txt` 且禁直接读答案。
⚠️ 实测发现：只加 SPEC + 同名方法**不够**——`call()` 先用 `names()`（来自 `specs()`）做白名单，缺注册直接报「未知本地工具」（`tools_local.py:277`）。这条已写进工单 §3.2 的三处改动。

**3) `tools/make_misc_kit.py`**：一键出 kit（`src/ tests/ config/` + Dockerfile + 工单 + `RUN-TESTS.md`），自带泄密断言（禁 `.env`/`runs/`/`HANDOFF.md`/`PROGRESS.md`/`mcp-tools.json`/密钥形态）与 `--verify`（在 kit 内实跑单测）。实测 269 KB，kit 内 48 PASS。
⚠️ 泄密扫描第一版把 `os.environ.get("..._API_KEY")`、`api_key=api_key`、文档里带反引号的说明全判成密钥 → 改成「只认带引号的字面量 + 跳过代码取值行」。

**4) `tools/misc_contract_selftest.py`**：验证「验证器」本身——沙箱复制仓库、造 5 种交付、断言该过过该挂挂。实测 5/5：clean 跳过 / good 3 项全过 / cheat、noshell、badtool 各命中对应断言。
⚠️ 坑：Windows 沙箱下 `tempfile.mkdtemp()` 落在平台受限临时区，其下建子目录 `PermissionError`（WinError 5）→ 沙箱改放 `dist/_selftest_*`。

**5) 工单升级 v2**（`docs/misc-module-brief.md`）：补 §3.4 交付契约（evidence 必填/逐字、账本同参拦截）、`allow_shell` 默认 false 必须覆盖、夹具必带 `solve.py` 确定性参考解（接单方没有 key，跑不了 Crew）、基数口径「35 case / 47 PASS」→「36/48」、打包改用脚本并禁止带 `HANDOFF.md`。

### 3.13 misc 模块接入（外包交回 → 审查 → 合并，用户批准后执行）

**基线：37 个 case / 49 条 PASS，全绿**；`compileall` 干净。交付包 `dist/misc-module.tar.gz`（53 KB / 28 文件）经静态审计 + 独立解码复核 + 一次性沙箱实跑后合并进主仓库。

**核验结论（详见 `docs/review-misc-module.md`）**：四条验收标准全部真实满足——`compileall` OK、`49 passed, 0 failed`、契约 7 项、3/3 夹具 `solve.py` 复现 `expected.txt`。0 非标准库、0 `eval/exec/os.system/socket/subprocess(shell=True)`。提示词与三条行为契约对齐（evidence 逐字 + 账本同参拦截 + 16000/6000 口径），且因文本已含 "evidence"/"report"，`EVIDENCE_RULE`/`HANDOFF_RULE` 不重复注入、`COMPANION_RULE` 正常补（无 "PLAN"）。

**发现并处理的坑**：
1. **P0**：外包的 `roles/misc.json` 落盘后**框架不会读它**（只读 `config/agent.json` 的 `extra.roles`，`crew.py:224`）——不合并进 config，真跑时 misc 仍是旧配置（`allow_shell:false` + 无提示词 + 无三工具）。已程序化合并 line 34。
2. **install.py 验收器在 Windows/GBK 下必挂**：`subprocess.run(text=True)` 用系统编码解自己输出的 UTF-8 中文 → `UnicodeDecodeError`（安装成功、自验收崩）。我方绕过它直接跑测试；已列入反馈。
3. **708 行 vs 工单 ≤400 红线**：纯新增模块、不碰框架既有路径，获豁免（用户批准）。
4. **patch 换行符**：接单方 `.new.py` 是 CRLF、我方源是 LF → 逐字符一致、字节不一致，纯换行差异，Python 两边都接受。

**合并动作**：两处框架 patch（`tools_local.py` +22、`crew.py` +16/−1，与 `.new.py` 逐字符一致）；新增 `src/ctfy_agent/tools_misc_ext.py`（3 工具）、`roles/misc.json`+`misc.system.txt`、`tests/smoke_case_misc.py`、`tests/fixtures/misc/01..03/`；`tests/smoke.py` 注册 `case_misc_ext`；`config/agent.json` 合并 misc。

**防漂移（选项 B）**：`case_misc_contract` 加断言「`config/agent.json` 的 misc == `roles/misc.json`」——契约用例只验证 fragment，这条把真跑读的活配置也拉进回归网；`make_misc_kit.COPY_DIRS` 加 `"roles"`、`RUN_TESTS` 基线改 49。





---

## 4. 当前代码状态

- 测试 **49/49 全绿**、共 **37 个 case**（A0 `case_challenge_meta`/`case_hint_scope`；A1 `case_ledger`/`case_ledger_crew`；A2 `case_report_evidence`/`case_plan_companion`/`case_delegate_objective`；外包准备 `case_misc_contract`；外包接入 `case_misc_ext`；审计回归 `case_audit_regression`），`compileall` 干净。
- 第四轮（全量代码审计 + 修复，2026-10-25；详见 `docs/audit-2026-10-25.md`）：三个探针实测坐实 5 个缺陷并全部修复——#1 非法端口让 `normalize_url` 抛 `ValueError` 击穿整个 run（`_ledger_gate`/`_ledger_after` 均在 try 外）→ 归一化降级 + gate/after 包 try；#2 delegate 去重键丢 `objective`（A2 回归，同 role 所有派活撞同一键）→ 归一化改读 objective；#3 `note`/`report` 进账本伪造「推进」信号 → `_ledger_after` 对 `NO_DEDUPE` 跳过；#4 HTTP 缓存跨题不清理 → `solve` 入口 clear；#5 账本摘要双重注入 → 删 `run_role` 前置。补 `case_audit_regression` 反向断言。**教训：A1/A2 交界处是缺陷高发区，反向断言（不同目标不判重、归一化异常不击穿）必须进单测。**
- 第三轮：对 CTFARENA-035 尸检报告复核后，按确认的修复清单落地 6 项（见下）。
- 第二轮审计（只给结论不改码）后，按「先复核再修」修复以下项：

**正确性（高价值）**
- `crew.py` — 角色**深合并**（B2，部分覆盖内置角色不再吞默认）；协调者循环接入 `_compact_history` 滑窗（B1）；协调者 chat/delegate 的 **LLMError 隔离**（B4）；预算预检（B8，超限题零成本退出）；实例释放改 **try/finally 且提交成功后也释放**（B7/B20，加 `keep_instance_on_success` 逃生口）；`_brief` 改实例方法 + 单次调用（B6/R2）；rounds 下限 `max(1,…)`（B5）；delegate 结论只记 800 字摘要（B3）
- `solver.py` — 提交工具名**动态化**（B13：`SYSTEM_PROMPT` 写死的 `submit_flag` → `try_resolve("submit_flag")`）；`usage`/`tokens_spent` 改**本轮 delta**（B9/B16，不再算累计值）
- `tools_local.py` — grep **边扫边截到 150 条**（B11，不 OOM）+ 正则长度上限 256（B12 尽力缓解）+ 非法端口抛 `ToolError`（B14）
- `auto.py` / `cli.py` — 批量记账改用 `tokens_spent`（B9，重试不重复计入）

**复核下调未改**：B10（breaker 只数异常是设计意图，B8 已消掉浪费）；S1（shell RCE 根治等容器丢权）；B12 残余（stdlib re 无超时，彻底根治需换库/子进程超时）。

- 本轮改动文件：`crew.py` / `solver.py` / `tools_local.py` / `auto.py` / `cli.py` / `tests/smoke.py`（+3 测试：角色深合并、提交工具名动态化、提交成功后释放实例；另在 hardening/auto 补 grep 截断、正则长度、端口、批量 delta 断言）
- 此前已落地（更早会话）：crew 多智能体、auto 批量、Windows shell PATH 修复、https:443、滑窗、grep 等。

**第三轮（尸检复核后的 6 项修复，本轮）**——文件 `tools_local.py` / `solver.py` / `crew.py` / `tests/smoke.py` / `Dockerfile`：

1. `http_request`：`offset` 参数（5000 字符窗口，因 `MAX_TOOL_TEXT=6000` 留转义余量）+ **Range 头本地化成 offset**（不再转发、416 消失）+ `local_cap`/`past_end`/`next_offset` 标记 + 恒读 2MB + `Accept-Encoding: identity`；`MAX_GREP_MATCHES` 150→**30**（整张匹配表塞进 6000 可见上限）。
2. `run_role`：步数/预算耗尽时**合成报告**（最近动作清单 + 最后思考），不再只交一行占位符。
3. `_ensure_instance`：**幂等启动**（先 `list_instances` 查、客户端按 challenge_id 过滤）+ 429/5xx **指数退避** + 模型侧重复 start **拦截**（已在运行就直接回）。
4. shell：容器侧 `Dockerfile`（python:3.12-slim + curl/grep/coreutils/ca-certificates）；本地 Windows 侧整词黑名单拦截 Unix 内建 + python3/heredoc/单引号提示。
5. 提示词：web 角色补 SPA grep 套路；`SYSTEM_PROMPT` 补大响应 offset 指引。
6. body 缓存：crew 级 `(host,path?query)` 缓存（跨 delegate 共享）+ **重复读桩**（省 token 的真正来源）+ 非 GET 清同 host + 16 条/8MB FIFO 上限。

⚠️ 均为**未经真实 run 验证**的新代码（grep 功能此前也从未在真实 run 里跑过）；下一步优先重跑 CTFARENA-035 验证。

---

## 5. 阻塞（需用户处理）

| 事项 | 原因 | 用户动作 |
|---|---|---|
| 镜像构建/推送 | 本地无 Docker / 无 WSL 发行版 / 非管理员 | 装 Docker Desktop，或提供能 build+push 的 CI/GitHub 账号 |
| provider 密钥 | 平台「我的 Agent」页面需要账号 | 添加所用模型 provider 的 API 密钥 |

审核是异步的 = 关键路径，建议尽早把镜像注册 + 审核申请走起来。

---

## 6. 续接命令（恢复工作）

```powershell
# 环境
$env:PYTHONIOENCODING = "utf-8"
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

# 自测（应 47/47 绿）
& $py tests\smoke.py

# A0/题型线索验证（只握手 + get_challenge，零 token、不起实例）
& $py runs\verify_a0.py

# A1/账本回放：拿历史转录量化「能拦多少重复调用」（零成本）
& $py runs\ledger_replay.py

# 重置某题累计 token + 状态（budget 跨 run 累计，重跑前必须重置）
& $py -c "import sys; sys.path.insert(0,'src'); from pathlib import Path; from ctfy_agent.state import Store; s=Store(Path('runs/default')).load(); ch=s.require('CTFARENA-035'); ch.tokens={}; ch.status='new'; ch.instance_id=''; ch.instance={}; s.save(); print('reset ok')"

# 重跑（三件套修复后应显著降 token）
cmd /c run.cmd crew CTFARENA-035
```

---

## 7. 下一步（按优先级）

1. 重跑 CTFARENA-035，验证「护栏 + 滑窗 + grep + offset/缓存 + 幂等启动 + 合成报告」是否让 easy 题在预算内解出（这轮 6 项均未真实 run 验证）。
2. 通了就批量跑 10 道 web 记录基线（成功率/步数/token/失败分类）→ `run.cmd auto`。
3. 用户装好 Docker 后：做最小 hello 镜像打通「注册 → 审核 → 密钥」三道门。
4. 容器适配层：entrypoint（task.json → Crew → result.json）+ Dockerfile + LLM 客户端切网关（含流式）。
5. pwn 视时间（2 道 SAMPLE 栈溢出起步）。

---

## 8. 附：关键文件位置

- 项目根：`E:\ctfy_project`
- 配置：`config/agent.json`（角色/模型/上限）、`.env`（token / base_url）
- 运行状态：`runs/default/state.json`（累计 token 与状态）、`runs/default/transcripts/CTFARENA-035.jsonl`
- 契约源码：`.tools/ctfy-src/ctfy/core/agent_brief.py`、`.tools/ctfy-src/ctfy/eval/{harness,llm,external}.py`
- 参赛手册：`runs/handbook-zh-CN.md`（zh 权威）、`runs/handbook-en.md`（en 已过时）

---

## 9. 本轮进度存档（暂停于「功能」阶段，容器适配层未做）

> 状态基准：**smoke 55 passed / 0 failed**，`compileall` 干净。用户排序：**先做完功能，再补镜像**。

### 已完成（本会话，已改代码 + 已测）

1. **裁工具 28→13**：web 平台面改 9 工具白名单（`config/agent.json` + `crew.py::DEFAULT_ROLES`），
   工具 schema 9,205→3,624 est（−61%）；删掉的 15 个仍在 `PLATFORM_SCOPE`。
2. **逐调用 usage 落盘**：`solver.solve` / `solver.run_role` / `crew` 协调者三处 transcript 记录加 `"usage"`（真机已验证，含 cached_tokens / reasoning_tokens）。
3. **competition_id membership 兜底**（P1）：`solver._competition_id(challenge_id)` 按「题目属于哪场」反查 + 写回 meta 缓存。
   修掉 `start_instance` 报 `competition_required`——`crew` 路径 + 新 run 目录 meta 为空必踩（容器就是全新目录）。
4. **http_request 会话 cookie jar**：`Set-Cookie` 自动回传 + 显式 Cookie 优先 + 缓存 key 含 cookie（登录前后不串会话）。
5. **预算/上下文**：`max_tokens_per_challenge` 500000→**800000**（对齐 B1 方案），新增 `max_context_tokens` **32000**（原 DEFAULTS 16000）。
6. **文档**：`docs/borrowed-techniques.md`（Cybench 口径 + 可借鉴清单）、`docs/review-token-analysis.md`（实施结果 + 首次解出）。
7. **`http_request` 加 `diff=true`**（IDOR 枚举用）：`diff=True` 时**跳过缓存读**强制重抓，取缓存里
   **同 netloc + 同 path（忽略 query）最近一条**当基线，`difflib.unified_diff` 行级比对，只回差异行，
   截断 4000 字符；输出 `{status, headers, diff, added_lines, removed_lines, baseline_had, truncated}`，
   无基线退回普通窗口并标注 `baseline_had=false`。配套两点：
   - `solver._ledger_gate` 对 `diff=true` 放行（`ledger.normalize_args` 不看 diff，不然同 URL 重查会被
     「完全相同的调用」拦掉，功能形同失效）；
   - `diff` 结果照常写回缓存，枚举时基线链条自动前进。
   新增 `tests/smoke.py::case_http_diff`（6 组断言：schema、首次无基线、改 id 三行差异、同 URL 无差异
   且仍联网、写回缓存、新 path 兜底、400 行全变截断）。http_request schema 1,324 字符（diff 段 182 字符）。

### 真机实测（关键数据，本轮）

- 网关 `ai.paratera.com/v1`：85 模型，5 个角色模型全在；平台 ctfy 3.4.6，token 有效。
- **006 解出**（`submitted` / `flag_accepted`）：430,873 token / 56 调用 / 7 回合 / ~11min；
  路径 SSRF→`file://` LFI→shell 跑 Python 批量读→flag；web prompt 占 92%，cached_tokens 90%。
- 001 stalled 80,018/7calls、035 stalled 504,953/50calls（旧，A1-A3 之前）。
- 003 跑到 135,464/23calls 被环境杀掉（会话切换，非失败），实例已清理，平台无残留。
- 实测单次 prompt 中位 7,739（035 时代 9,980）；最高 15,229 → 16k 滑窗丢证据是真实瓶颈，故上下文上调 32k。

### 进行中 / 未完成

- method_cards 扩 23→50+：需 Cybench 轨迹日志（Google Drive，沙箱不可达）。
- web.max_steps 校准：同上（实测 10 步常出不来 report，退化「跑了 N 步未给结论」）。
- **B1 容器适配层**：`agent_main.py` 不存在、`Dockerfile` 的 `ENTRYPOINT` 注释。用户明确放最后。

### 续接命令（恢复后）

```powershell
$env:PYTHONIOENCODING = "utf-8"
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "E:\ctfy_project\src"
# 自测（应 55/55 绿）
& $py tests\smoke.py
# 真机跑法（注意 --config / --run-id 是全局参数，必须在子命令之前）
& $py -u -m ctfy_agent --config E:\ctfy_project\runs\_probe_config.json --run-id probeN crew CTFARENA-006
```

## 10. AI 安全纵向进度（2026-10-26；**本节是最新事实，与 §9 及更早的陈旧陈述冲突时以本节为准**）

### 状态：M1–M4 完成，M5 待批准

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | `chat` 多轮对话工具（工具侧历史 / SSE 聚合 / cookie / 账本键含 message） | 完成，审核通过 |
| M2 | `ai` 角色接入（`roles/ai.json` + `config/agent.json` + 协调者路由 + 契约/接线用例） | 完成 |
| M3 | 方法卡 17 张（10 ASI + 7 防御族）+ 三池词表对账 + 4 道离线夹具 | 完成 |
| M4 | 容器契约 e2e（AI 题 task.json → run_agent → result.json）+ 陷阱页夹具 | 完成 |
| M5 | 真机 2/2 解出：CTFARENA-038（135k）+ 039（210k），Kimi-K2.7 | **已完成，待确认** |
| M6 | 校准 + 镜像重建/push + PROGRESS/HANDOFF 全量同步 | 未开始 |

### 关键事实（M5 前必须知道）

- **容器内单模型**：`agent_main._build_config` 会 pop 掉所有角色的 `model`，网关按 `LLM_MODEL` 统一——
  `config/agent.json` 里的多模型名只在本地 CLI 路径生效；选型要兼顾对话诱导与页面分析。
- **三池规模**：CTFARENA-038..047（10 题，自然语言答案）、AGENTBENCH-001..005（5 题）、
  AGENT-DEFENSE-001..037（**37 题，flag 形态**，陷阱 tag 60+ 种）。共 52 题。
- **防御卡是角色无关的**：`_method_hint` 按 category/tag 注入，web 专家处理陷阱页时同样拿到防御卡。

### 验证命令（当前应 78/78 绿）

```powershell
$env:PYTHONIOENCODING = "utf-8"
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "E:\ctfy_project\src"
& $py tests\smoke.py              # 79 passed, 0 failed
& $py tests\smoke_ai_agent.py     # 1 PASS（AI 容器契约）
& $py tests\smoke_ai_fixtures.py  # 4 道夹具
```

### AI 纵向新增/改动文件

`src/ctfy_agent/method_cards.py`（AI_CARDS/AI_TAGS）、`src/ctfy_agent/tools_local.py`（chat）、
`src/ctfy_agent/solver.py`（_method_hint 门控兜底）、`src/ctfy_agent/crew.py`（协调者路由/ai 角色）、
`roles/ai.json` + `roles/ai.system.txt`、`config/agent.json`（roles.ai）、
`tests/smoke_ai_chat.py` / `smoke_ai_role.py` / `smoke_ai_fixtures.py` / `smoke_ai_agent.py`、
`tests/fixtures/ai/`（pool_tags.json + 4 道夹具）、`docs/ai-plan.md`（计划与逐里程碑证据）。


---

## 本轮：多问题支持 + 真机复验 + 容器验收脚本（2026-10-25 晚）

### 代码改动（78/78 绿，`tests/smoke.py` 连跑 3 次 exit 0）

| 文件 | 改动 | 为什么 |
|---|---|---|
| `crew.py` | `_fetch_challenge` / `_question_ids` 辅助；`_brief` 增「多问题题」块；`submit` 分支：`challenge_fully_solved=false` 时不收尾、轮换 `default_question_id`、提示「答完前不要 done」 | CLI/MCP 路径原先在门控/多问题题上只交第一题就收尾（SAMPLE-001 实测只拿到 `flag`、漏了 `slug`） |
| `crew.py` | 新增 `UNAVAILABLE_RULE`，无条件拼进所有角色 system（含 `agent.json` 自定义提示词） | 实测 SAMPLE-004 是 `access=vpn` 的 `*.internal` 服务，本地 DNS 不解析；模型改去翻本地源码，白烧 118k token |
| `solver.py` | `_submit` 文案中性化（「平台确认该答案通过」）；`_start_with_backoff` 与 `_execute` 兜住 MCP 异常；限流持续时附「配额=1，先 list_instances」提示 | 见下方平台坑 |
| `mcp_client.py` | `open()` 与响应体读取都捕获 `TimeoutError` → `McpError(retryable=True)` | 读超时不是 `URLError`，原先直接抛穿 `_post` → **整条 agent 崩掉 = 不写 result.json = 0 分** |
| `tests/smoke.py` | 新增 `case_multi_question`、`case_stdlib_only` | 锁住多问题语义 + 「纯 stdlib」容器契约（有第三方 import 就在镜像里 ImportError） |
| `tests/smoke_ai_role.py` | `ai_wiring` 工厂改为按测试自己钉的 `AI_TEST_MODEL` 分发 | 活配置里 ai 与协调者已同为 `Kimi-K2.7`，原先按 `"Kimi-K3"` 分发会让 ai 专家拿到协调者脚本（`[未知工具] submit`）——这是**测试假红**，不是产品 bug |
| `.dockerignore` | 排除 `.tools/`(50MB)、`misc-bank/`(18MB)、`.venv/` | 主办方要求镜像别太大；这些不该进构建上下文 |
| `tools/container_check.py` | 新增容器验收脚本：构建 → 体积 → 目录洁净度 → cwd 无关性 → 五条契约路径冒烟 | 无 Docker 时用 `--local` 已验：五条路径全部 exit 0 且 `stop_reason` 正确 |

### 真机证据

- **SAMPLE-001（web）多问题语义**：新实例下载 `setup.txt` 取当前 slug → `submit_answer{question_id:"slug", answer:<slug>}` →
  `{"correct":true,"question_id":"slug","challenge_fully_solved":true}`。证明 `challenge_fully_solved` 就是权威完成信号。
- **SAMPLE-010（reverse, easy）端到端解出**：rev 专家从 per-team 工件 `licencecheck.py` 拆出 `_STRIP_A/_STRIP_B` 拼回
  `FLAG{e7c77a54eed5e2a9fa5e73e505d7da04}`；平台回执 `correct:true, solve_rank:15, challenge_fully_solved:true`；
  148k token / 20 calls / 协调者 2 步。转录 `runs/default/transcripts/SAMPLE-010.jsonl`。

### 平台坑（下次别再踩）

1. **实例配额 = 1，且「Rate limited by upstream API」多半是配额被占而不是时间限流**。`job_kill` 杀掉 crew 进程时
   `finally` 里的停实例不会跑 → 泄漏实例占住唯一名额 → 之后所有 `start_instance` 全报限流。清理后立刻成功。
   排查动作：`list_instances` → `stop_instance` → 重试。
2. **per-team 工件必须有运行中实例才能下载**：`download_challenge_attachment` 返回
   `http_409: This attachment is generated per team and only exists once you have a running instance`。
3. **slug / flag 是随实例生成的**，附件写明 `Generated for instance: <uuid>`；换实例后旧值作废，必须重新下载。
4. **`access: vpn` 的 `*.internal` 服务本地不可达**（DNS 不解析）；`config_url` 为空时没有客户端配置可用。
   遇到就按 `UNAVAILABLE_RULE` 立刻交卡点，别翻本地文件系统。
5. `my_solved_question_ids` / `my_team_*_solved` 实测不可靠（我们提交成功后仍为空）——只信 `submit` 回执的
   `challenge_fully_solved`。

### 容器契约静态验证（无需 Docker）

- 纯 stdlib：23 个文件 / 31 个顶层 import / **0 个第三方**（已固化为 `case_stdlib_only`）。
- 配置定位：`config.py` 用 `Path(__file__).resolve().parents[2]` → `/ctfy/config/agent.json`，与 Dockerfile 的
  `COPY src/ /ctfy/src/` + `COPY config/agent.json /ctfy/config/agent.json` 精确对齐，**不受 cwd=/ctfy/work 影响**。
- 失败兜底：`agent_main` 所有分支都 `return 0`；五条契约路径（dry_run / 缺 task / 非 instance / 缺凭证 / 无 MCP 端点）
  本地实测均 exit 0 且写出合法 `result.json`。

### 待办（唯一硬阻塞）

**镜像无法在本机构建/验证**：无 Docker Desktop、无 podman/nerdctl、无 WSL。
已备好 `tools/container_check.py`；Docker 装好后执行（在 `E:\ctfy_project`）：

```powershell
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
& $py tools\container_check.py            # 构建 + 全部验收；通过即可上传
& $py tools\container_check.py --no-build # 复用已构建镜像复验
```
