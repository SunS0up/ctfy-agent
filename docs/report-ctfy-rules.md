# CTFy 平台「接入规则」调研报告

**调查对象**：https://ctfy.tsinghua.edu.cn（首届 CCF 网络安全智能挑战赛暨「京华杯」TPCTF 2026 官方平台）
**调查时间**：2026-10-25 11:00 CST
**调查方式**：只读抓取（HTTP GET + 前端代码块解析），未登录、未提交任何表单、未创建账号
**报告文件**：`docs/report-ctfy-rules.md`

---

## 0. 结论先行

1. **站点上不存在名为「介入规则」的页面。** 与这个说法对应的规范有两处：一是平台标准 **CTFY-0001《The ctfy agent protocol》**（智能体**接入**协议，v2，2026-10-13 修订），二是 `task.json` 里的 **`rules_of_engagement`** 字段（交战规则，仅 `engagement` 模式有值）。本报告两者都覆盖，重点在前者。
2. **CTFY-0001 是一份 3.9 万字符的强约束契约**，不是说明文档。它规定环境变量、工作目录、5 种 run_mode、`result.json` 格式、资源上限、退出码，并明确「不写结果文件 = 判为 failed」。
3. **合规成本极低但红线很硬**：`/ctfy/work/result.json` 必须存在、必须 `exit 0`、`stdout` 不被读取。这三条是最小合规集（§3.2）。
4. **赛事层面有 3 条会直接改变设计的事实**：`internet_policy=deny`（比赛期间无外网）、`max_instances_per_team_effective=1`（同时只能有 1 个实例）、`authoring_enabled=true` 且 3 个出题窗口（智能体要**既出题又解题**）。
5. **规则全文有缺口**：赛事手册 §八写「赛事规则见智能体赛道附件」，但附件接口返回空数组，规则文档目前**未公开**。

---

## 1. 站点结构与规则的三个层级

### 1.1 站点形态

首页是单页应用（`/assets/index-uEKiPrO5.js`，534 KB），所有内容由 `/api/v1/*` 驱动。直接抓 HTML 只能看到 `<div id="root"></div>`，必须走 API 或解析前端代码块才能拿到正文。这一点决定了**任何"打开网页看规则"的做法都会看到空白页**。

平台元信息（`GET /api/v1/meta`）：

| 项 | 值 |
|---|---|
| 平台版本 | 0.7.0（commit `d7f8d0a`，开源：github.com/wangyihang/ctfy） |
| 题库 | 2848 道 / 20 个 suite / 2437 个环境 |
| 实例 TTL | 默认 10800s，最大 10800s，可选 1800/3600/10800 |
| 赛事 | 进行中 2 场、即将开始 4 场 |

### 1.2 三层规则（本报告的结构）

| 层级 | 载体 | 性质 | 是否公开 |
|---|---|---|---|
| **L1 平台标准** | `/docs/standards/CTFY-0001`、`/docs/standards/CTFY-0002` | 技术契约，注册镜像的准入条件 | 公开（内嵌在前端代码块，无独立 API） |
| **L2 赛事手册** | `GET /api/v1/competitions/{id}` 的 `handbook` 字段 | 赛事章程（组织、赛道、赛程、奖项） | 公开 |
| **L3 赛事规则附件** | 同上比赛的 `attachments` | 「赛事规则文档」本体 | **当前为空** |

L1 是硬的（不满足就跑不起来），L2 是软的（不影响运行，影响资格与判罚），L3 缺失（等于判罚细则未知）。

---

## 2. L1：CTFY-0001 智能体接入协议（核心）

> 原文提取件：`runs/site/standards/CTFY-0001.md`（943 行，正文 39,174 字符）
> 编号 CTFY-0001 / 状态 Active / 版本 2 / 首发 2026-08-27 / 修订 2026-10-13

### 2.1 设计哲学（决定了很多细节）

原文开篇："平台对你的镜像内部**不做任何假设**：不限语言、不限框架、不限基础镜像、不限 entrypoint 约定。它启动你的容器，告诉你要做什么，然后读回**一个文件**。"

所以整套契约收敛到一句话：**读 `/ctfy/work`，写 `/ctfy/work/result.json`，退出码 0。**

### 2.2 环境变量（§1.1）

| 变量 | 含义 | 备注 |
|---|---|---|
| `CTFY_PLATFORM_URL` | API 基址，如 `https://ctfy.example` | dry run 时为空串 |
| `CTFY_TOKEN` | bearer，用法 `Authorization: Bearer $CTFY_TOKEN` | dry run 时**不存在** |
| `CTFY_EVAL_CHALLENGE_ID` | 哪道题 | |
| `CTFY_EVAL_COMPETITION_ID` | 哪场比赛（可空） | |
| `CTFY_EVAL_MAX_TURNS` | 自己 agent 循环的软预算 | **不存在 = 无预算**（不是 0） |
| `CTFY_WORK_DIR` | 工作目录 `/ctfy/work` | 简报在这里，结果也写回这里 |
| `LLM_FLAVOR` | `anthropic` 或 `openai` | 决定用哪套 SDK 变量 |
| `LLM_API_KEY` | 模型密钥 | **可能是每轮句柄，也可能是空** |
| `LLM_MODEL` | 要请求的模型 id | 平台答案以此为准 |
| `LLM_BASE_URL` | 端点覆盖 | 缺省 = SDK 默认 |
| `CTFY_EVAL_DRY_RUN` | `1` 表示这是镜像体检，不是做题 | 平时不存在 |

三条原文警告，都是踩过坑写下来的：

- **`LLM_API_KEY` 可能为空，这是真实状态而不是 bug**——意思是本次运行没有人出钱。要**大声失败**，绝不回退到自己准备的 key：「悄悄花别人账户的一次运行，比停下来更糟」。
- **`LLM_BASE_URL` 和 `LLM_API_KEY` 是一个决定**。给了 base URL 就必须用，否则把 key 发到默认端点，报错会伪装成"凭据无效"。
- 平台还会把同一组值写成各家 SDK 自己的变量名（`ANTHROPIC_API_KEY`+`ANTHROPIC_AUTH_TOKEN` / `OPENAI_API_KEY`，以及 `*_BASE_URL`、`*_MODEL`）。**只给一套，绝不给两套**——否则按"哪个变量存在"选 provider 的库等于抛硬币。

> 关键点：`anthropic` flavor 下，`ANTHROPIC_API_KEY` 会被 SDK 变成 `x-api-key` 头，`ANTHROPIC_AUTH_TOKEN` 变成 bearer；Anthropic 官方端点两者都收，但**兼容端点只认 bearer**，只带前者会得到"未登录"，看起来像账号问题而不是头送错了。

### 2.3 工作目录 `/ctfy/work`（§1.2）

启动时目录里就有三份文件，`result.json` 由你写：

```
/ctfy/work/task.json    机器可读的契约
/ctfy/work/README.md    同一件事的散文版，给模型读
/ctfy/work/mcp.json     平台自身工具的 MCP 客户端配置
/ctfy/work/result.json  ← 你写这个
```

**硬约束一：这个目录就是容器的 cwd。** 原文明确：

> `ENTRYPOINT ["python", "agent.py"]` 会用工作目录解析文件名，**起不来**；要写 `ENTRYPOINT ["python", "/app/agent.py"]`。`ctfy agent check` 会在注册前指出（报 `relative_entrypoint`），dry run 直接拒绝这种镜像。程序用相对路径打开的数据文件同理。

这与我们此前从平台源码推断的结论完全一致，但现在有了**规范级依据**。

**硬约束二：目录会被打包带走。** 容器停止时 `/ctfy/work` 全量归档（连同 Claude Code 会话、stdout/stderr），run 的所有者可下载查看。所以：

> **不要把自己的凭据写进这个目录。** 平台从不写（`mcp.json` 里只有 `${CTFY_TOKEN}` 占位符），你也不该写——这个目录的寿命比容器长。

**`mcp.json` 的价值**：整个玩家 API（报名、列题、起停实例、提交答案、看榜、AWD 抓包）都以 MCP 工具形式挂在 `/api/v1/mcp/`，同一个 bearer。所以接入就是一行：

```bash
claude --mcp-config "$CTFY_WORK_DIR/mcp.json" -p "$(cat "$CTFY_WORK_DIR/README.md")"
```

不用 MCP 也行——REST 才是契约，MCP 只是投影。最便宜的连通性自检：

```bash
curl -sS -X GET "$CTFY_PLATFORM_URL/api/v1/me" -H "Authorization: Bearer $CTFY_TOKEN"
```

> 注意一个易错点：`mcp.json` 里的 bearer 写的是字面量 `${CTFY_TOKEN}`，不是值。会展开 `${VAR}` 的客户端直接可用；不会的必须自己替换，否则会得到 401，且看起来像 token 被吊销。

### 2.4 五种 run_mode（§2）

平台**自己**从题目和比赛推导 `run_mode` 并告诉你，不会问你的镜像支持哪种。

| run_mode | 场景 | 交付物 | 关键陷阱 |
|---|---|---|---|
| `instance` | 有基础设施的 jeopardy 题 | 每题一个答案 → `POST /api/v1/submissions` | `gated_questions > 0` 时，**每答对一题都要重新拉 `GET /api/v1/challenges/{id}`**，后面的题面此时才存在 |
| `qa` | 纯选择题（413 道：SecQA/MMLU-CS/CTI-MCQ） | `POST /api/v1/qa/submissions` | 没有实例可起 |
| `defence` | AWD+，给你一台要守的机器 | 补丁 → `POST /api/v1/patches` | 该接口返回**回执不是判决**；`pending` = 已受理。当结果处理会导致重复提交撞上限 |
| `awd` | 经典攻防，每轮换 flag | 攻击抢 flag + 防守自己那台 | **只启动一次，不按轮重启**；提前退出记为 `incomplete`；镜像必须自带 ssh 客户端 |
| `engagement` | GAUNTLET 企业靶场 | 一份报告 `PUT .../report` | `target_instance` 有值时靶场**已在跑**，禁止再 `POST /instances` |

`instance` 模式的 `task.json` 样例（原文 §2）：

```jsonc
{
  "schema_version": 2,
  "run_mode": "instance",
  "challenge": { "id": "CTFARENA-001", "name": "DocuVault", "description": "…" },
  "competition_id": "arena",
  "questions": [ { "id": "flag", "prompt": "The vault master secret" } ],
  "gated_questions": 2,
  "scope": [],
  "rules_of_engagement": [],
  "deliverable": "an answer per question, submitted to POST /api/v1/submissions",
  "platform_url": "https://ctfy.example",
  "max_turns": 40,          // null 表示没设预算 —— 永远不是 0
  "result": { "path": "/ctfy/work/result.json", "stdout_fallback": false }
}
```

`awd` 模式的几条硬规则（若将来接 AWD 表演赛才用得上）：

- 比赛时长 = `starts_at + tick_seconds × tick_count`（`get_awd_match` 给）。
- **进程退出 = 该场比赛结束**，即使还有轮次；提前退出会被记为 `incomplete` 而非 `succeeded`。
- 每轮 flag 会轮换，手里的 flag 立即失效，要重打。
- 防守的正确做法是「拿到自己那台的 shell，就地修」，`POST /patches` 只是沙箱判分，**不影响**正在被探测的那台。
- `open` 暴露模式下，**别的队能直接 ssh 你的机器**，默认口令 `changeme`；不轮换就是敞开大门。
- 镜像必须带 ssh 客户端（其余四种模式都不需要，很容易漏），`open` 模式还要 `sshpass`。

`engagement` 模式（GAUNTLET）：

- 有 `scope`（域名/网段）和 `rules_of_engagement`（**这就是字面意义上的「介入规则」**），没有题目列表，静默判分。
- `target_instance` 非空 → 靶场已起好、本容器已在其网内，**再起实例会花掉团队配额并测一个连不上的靶场**。
- 报告可反复 `PUT` 覆盖，关窗前的最后一版才算数；平台在**每次保存**时就从正文里抽证据，所以边写边得分。

### 2.5 结果文件 `result.json`（§3）

```jsonc
{
  "schema_version": 2,
  "answers": { "flag": "ctfy{...}" },   // 交给平台，由平台替你提交
  "solved": ["flag"],                   // 你答对的 question id
  "fully_solved": false,
  "turns": 17,
  "tool_calls_made": 41,
  "input_tokens": 120000,
  "output_tokens": 8000,
  "stop_reason": "done",
  "writeup": "## What I did\n\n…"       // 可选
}
```

要点：

- **字段全部可选**，`schema_version` 也 optional（但缺了会在一致性检查里记一条 advisory）。缺版本今天不花钱，第一次破坏性变更时要命。
- **`answers` 是最省事的通道**：填进去，运行结束时平台替你逐条提交，走的是和人类选手同一个判分器、同一个错次上限。但原文强调「运行中提交才是更富的选择」，理由有两条：**能及时知道自己对不对**，以及**门控题只有提交后才能解锁**。
- **`solved` 不决定得分**：平台按自己的提交记录重写这个字段。列了但没提交过的 id 就是假的。
- **`stdout` 对用户镜像完全不被读取**——原文说这是"本页最重要的一句话"。平台自带镜像保留了一个 legacy 的 stdout 兜底，它把**整段 stdout 当成一个 JSON** 解析，所以**一行日志就能毁掉结果**，报错还会写成"produced no JSON rollup"（看起来像模型放弃，其实是管道问题）。以 `task.json` 的 `result.stdout_fallback` 为准，不要自己猜。
- **`writeup` 不是提交通道**：里面写 flag 不得分，判分只走 `POST /submissions`。超长是**截断而不是拒绝**。
- **不写文件 = 判 failed**，错误会指名这个路径；**非 0 退出 = failed**，与你写了什么无关。

### 2.6 最小合规实现（§3.2）

原文给的三步，值得原样记住——这是"能不能跑起来"和"能不能得分"的分界线：

1. 读 `$CTFY_WORK_DIR/README.md`，照做。
2. 往 `$CTFY_WORK_DIR/result.json` 写 `{"schema_version": 2}`。**哪怕只写 `{}` 也算合规**。
3. `exit 0`。

### 2.7 dry run（§3.1）

注册前的体检运行，带 `CTFY_EVAL_DRY_RUN=1`、`task.json` 里 `dry_run: true`。最省事的正确响应：

```jsonc
{ "schema_version": 2, "solved": [], "fully_solved": false, "stop_reason": "dry_run" }
```

写完 `exit 0`。注意：

- **忽略这个标志不算失败**——只要写了结果文件就过。
- **dry run 没有任何凭据**：`CTFY_TOKEN`、`LLM_API_KEY` 都不存在，`platform_url` 为空。这是故意的（未审核镜像不该拿到提交能力和预算）。处理方式是"**写完文件之后**再大声失败"。

### 2.8 平台保证 / 不保证（§4）

**保证**：

- 全 capability 丢弃 + `no-new-privileges` + 资源上限（默认 **8 GiB 内存 / 2 CPU / 512 pids**），实际值见 `task.json` 的 `limits`。
- **跑的是运营方审批过的那个 digest**，事后挪 tag 不会改变跑什么。
- `task.json` 在同一 `schema_version` 内只做兼容性加字段；破坏性变更才升版本。
- `/ctfy/work`、会话、双路控制台输出会被归档，**只有 run 的所有者能看**。
- 所有可重复的读都会告诉你"多久后再问值得"：实例状态带 `poll_after_s` 和 `Retry-After`（刚启动几秒、跑起来更久、就绪后更久），榜单带 `Retry-After`。**自建轮询要按平台说的睡**，因为全场共享一个请求预算。
- 分段赛事会明确告诉你暂停：start/reset/submit 一律返回 `403` + 码 `competition_paused` + `next_session_starts_at` + `retry_after_s`。**这是等待，不是题目坏了**——睡到那个时刻，别轮询（轮询烧模型预算什么也证明不了）。段结束时还在跑的东西会被停掉（`stop_reason: session_ended`），下一段重起即可，**已得的解保留**。码 `competition_not_in_session` 才是"赛事真的结束了"。

**不保证**：

- `stdout` 会被读。
- 整个任务都在 `task.json` 里（见 `gated_questions`）。
- `LLM_API_KEY` 一定有值。
- **其它任何东西可达**——"今天运行网络还通外网，但部署方可以关掉，禁止联网的赛事一定会关"，所以**依赖要装进镜像，不要在运行时下载**。
- `max_turns` 是数字（可能是 `null`，且刻意不会是 `0`——`0` 会让最常见的循环写成"一步不走"）。
- 任何 entrypoint 或用户。工作目录是 `$CTFY_WORK_DIR`，**相对路径的 entrypoint 起不来**。
- 有隧道 helper（`/usr/local/bin/wg-socks` 的挂载在任何部署上都是惰性的，不要把运行模式建立在这上面）。

### 2.9 版本与自查（§5、§7）

- **标准号 ≠ schema 版本**。`CTFY-0001` 是这个文档永久的名字；`schema_version` 是它当前描述的线格式。加字段不动 `schema_version`，只有"能让合规客户端误读"的变更才动。
- 历史版本可读：`/docs/standards/CTFY-0001/v1`。
- 注册前自查：`ctfy agent check ghcr.io/you/your-agent:v1`。它跑平台跑的那两半（读 manifest + 一次本地 dry run），且**复用平台自己的规则模块**，所以这里过了注册就会过。
  - **没有 docker daemon 时只做 manifest 那一半，并会明说**。而最常见的失败（不写结果文件）恰好只能靠有 daemon 的那一半发现。
  - 它**不设体积上限**——那是各家运营方的设置，不是标准属性。
  - `--json` 供 CI 用。

---

## 3. L1 第二份：CTFY-0002《The ctfy challenge package》

> 版本 1 / 首发 2026-10-14 / 修订 2026-10-23 / 正文 62,979 字符
> 提取件：`runs/site/standards/CTFY-0002.md`

这份是**出题包**规范（我们解题用不上，但下面 §5 会说明为什么它对我们突然变得重要）。章节结构：

1. 目录结构（根、平台会读的文件、保留名、分类要求、上传时拒收什么）
2. `metadata.yaml`（顶层键、questions、模式推断、question id、`requires:` 门控、题面与逐实例取值、上传对题目的要求、声明 `standard:`）
3. Compose（用哪个文件、顶层键、服务键、取值校验、资源与健康检查、网络拓扑、启动方式、AWD 游戏盒）
4. 答案投递（通道、`flag-rotator`、模板 `attachments/*.tpl`、`attachment-builder`、`seeder` 与 `params/`、什么永远不能携带答案）
5. 判分（`match:`、`exact`、`contains`、checker 与 `judge`、错次上限、出题人自己队）
6. 参考解与 smoke（solution、契约、在哪跑、smoke、日志、何时跑、本地自测）
7. 运行环境（网络、可执行位、打包、体积上限、构建时间与运行审核、共享环境）
8. 版本与修订（每次上传都是版本、替换待审、修订已批、玩家拿到哪个版本）
9. 谁能看见（审核、发布、进比赛、公开目录、评测池、署名、声明、进语料库）
- 附录 A：审计规则码；附录 B：上传拒收码

值得注意的修订条目（说明平台在收紧）：

- 2026-10-17：上传题目的整个实例受运营方设定的 CPU/内存预算约束（默认 1 核 2 GiB），按服务分摊，每服务 512 进程；包自己声明的 limit 只是**分摊权重**。
- 2026-10-18：各服务声明的内存之和不得超过该预算（`authoring_memory_over_budget`）；CPU 超出只是建议（`authoring_cpu_over_budget`）；没声明 limit 的服务不计入且会被提醒（`authoring_resources_undeclared`）。
- 2026-10-23：顶层 `volumes:` 只能为空，或只有 `driver: local`；`driver_opts`（可把命名卷变成宿主机 bind）一律拒收。

---

## 4. L2：赛事规则（TPCTF 2026 · Agent Track）

### 4.1 手册要点（`handbook`，中文 3,046 字）

- **定位**：首届 CCF 网络安全智能挑战赛暨「京华杯」，CCF 主办，网络与系统安全专委会组织实施，清华（网络研究院）+ 北大（计算中心）承办；技术支持为**清华网络研究院 CTFy 平台**。
- **智能体赛道定义**（原文）：
  - 同一法人单位、**不超过 4 人**组队；
  - 需编写**符合平台规范**的 AI 智能体程序，并在指定时间内提交至比赛系统；
  - 智能体连接赛事环境，「按照预设规则**独立完成网络安全挑战的出题与解题等全部攻防任务**，**运行期间无人工介入**」。
- **参赛对象**：全日制在校生（含高职高专/本科/研究生）+ 网安相关企业从业者，不限专业。
- **命题方向**：智能体赛道**仅限在线交互类**（Web / Pwn / AI 安全）；离线附件类（Crypto/Rev/Misc）不适用于本赛道。
- **赛程**：报名 2026-10-15 起；入围测试 11-14~15（初选 11-15 10:00–20:00）；正式比赛 11-21~22 每日 10:00–20:00（人机协同仅 22 日）。
- **容量上限**：智能体赛道 **150 支**、人机协同 **500 支**；超额则视情况在 11-14 加试，未加试或同分时**先报名者优先**。
- **考场纪律**（原文三条）：① 智能体赛道须按时提交程序，**比赛期间禁止人工干预**；② 人机协同每队 ≤4 人、遵守平台规则；③ **严禁任何形式作弊，一经发现取消资格**。
- **奖项**：智能体赛道冠军 2 万 / 亚军 1 万 / 季军 5 千；另有创新挑战 3 千、创新解题 2 千；一等奖覆盖到前 5%，二等 5–10%，三等 10–20%。
- **联系**：Discord `https://discord.gg/U4xz78QKzc`、QQ 群 `1053114117`，申诉在平台内提交。

### 4.2 平台侧参数（API 实际下发，比手册更硬）

| 参数 | 值 | 对设计的影响 |
|---|---|---|
| `phase` / `play_open` | `upcoming` / `false` | 尚未开赛 |
| `sessions` | 11-15、11-21、11-22，各 02:00Z–12:00Z（= 10:00–20:00 CST） | 三段，每段 10 小时；段间必须能停下再起 |
| `internet_policy` | **`deny`** | **比赛期间容器无外网**，依赖必须进镜像 |
| `max_instances_per_team_effective` | **1** | 同时只能有 1 个实例，起新实例前必须先停旧的 |
| `tcp_access` | `tls_sni` | 对外服务按 SNI 路由（不是裸 IP:port） |
| `session_end_mode` / `solve_env_mode` | `delete` / `destroy`（各 900s 宽限） | 段结束即删实例，解题环境不保留 |
| `authoring_enabled` | **`true`** | **要出题** |
| `authoring_review` | `human` | 出的题人工审 |
| `authoring_windows` | 11-21 的三段 2 小时窗口（10–12 / 14–16 / 18–20 CST） | 出题只能在窗口内提交 |
| `ordering` | `flags_solved` ↓ → `solved` ↓ → `last_solve_at` ↑ | 同分时**先解出者靠前** |
| `freeze_minutes` | `0` | 榜单不冻结，全程实时 |
| `hint_cost_mode` / 生效值 | `discount` / `record` | 提示只记录不扣分 |
| `challenges_withheld` | `true` | 赛前题目完全不可见 |
| `format` / `expects_report` | `questions` / `false` | 不是 engagement，不用交报告 |
| `registration_ends_at` | 2026-11-13 12:00Z（= 11-13 20:00 CST） | 报名截止 |
| `registration_review` | `true` | 报名需审核 |
| `max_team_size` | 4 | 与手册一致 |
| `anonymous_entry` | `forbidden` | 不允许匿名参赛 |
| `registered_count` | **177** | 已超 150 上限 → 大概率有加试 |

### 4.3 ⚠️ 规则缺口：赛事规则附件为空

手册 §八原句：「**赛事规则**：见智能体赛道附件中的赛事规则文档，大赛技术委员会保留赛事规则修改权与最后解释权。」

实际核查：

```
GET /api/v1/competitions/c0d8b4e3…/attachments
→ 200 {"competition_id":"c0d8…","attachments":[]}
```

**附件为空**，`handbook.attachments` 也是 `[]`。也就是说：**判罚细则、出题评分权重、封禁条款等目前都还没有公开文本**。这必须在上线前持续盯（报名审核通过后通常会放出，附件也可能只对已报名队伍可见）。

---

## 5. 与本项目的差距（可执行清单）

以下每条都在当前代码里核对过，不是推测。

### 5.1 阻断级（不改就跑不起来）

| # | 契约要求 | 现状 | 处置 |
|---|---|---|---|
| G1 | 读 `/ctfy/work/task.json` 拿题面与参数 | 全库 `grep CTFY_` 只有 `CTFY_TOKEN` / `CTFY_MCP_URL`；**完全没有 task.json 解析** | 新增适配层入口 |
| G2 | 写 `/ctfy/work/result.json` 并 `exit 0` | **没有**任何 result.json 写出逻辑 | 任何路径都必须写（含异常、超时） |
| G3 | `ENTRYPOINT` 必须是绝对路径（相对路径会被 dry run 拒绝，报 `relative_entrypoint`） | `Dockerfile` **没有 ENTRYPOINT**，且 `WORKDIR /ctfy` | 加 `ENTRYPOINT ["python","-m","ctfy_agent","agent"]` + `ENV PYTHONPATH=/ctfy/src` |
| G4 | 模型凭据来自 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | `config.py:227` 只读 `DEEPSEEK_API_KEY`；`config.py:27` 默认 `https://api.deepseek.com/v1` | 三者必须接；`LLM_BASE_URL` 有值就必须用（原文的"一个决定"） |
| G5 | `stdout` 不被读取（一行日志即毁） | `cli.py` 大量 `print` 到 stdout | 容器模式下所有日志转 stderr |

### 5.2 高危（能跑但会失分/超时）

| # | 契约要求 | 现状 | 处置 |
|---|---|---|---|
| G6 | `max_turns` 是预算，`null`=无预算 | `config.py:40` `max_llm_calls=20` 硬顶 | 以 `task.json.max_turns` 为准；`null` 时用保守值而非 20 |
| G7 | `LLM_FLAVOR` 可能是 `anthropic` | `llm.py:120` 只有 OpenAI `/chat/completions` | **需要你确认注册 flavor**；若是 anthropic 需另写客户端 |
| G8 | MCP 地址来自 `$CTFY_WORK_DIR/mcp.json` 或 `CTFY_PLATFORM_URL + /api/v1/mcp/` | `config.py:50` 读 `CTFY_MCP_URL`——**这个变量在容器里不存在** | 改从 mcp.json 解析，回退到手工拼 |
| G9 | `gated_questions > 0` 时每答对一题重拉 challenge | 无 | 提交成功后重新 `get_challenge` |
| G10 | 段结束/暂停会 `403 competition_paused` | 无处理，会被当成题目失败 | 识别该码后按 `retry_after_s` 睡 |
| G11 | `internet_policy=deny` | 依赖 stdlib-only，**这条我们天然合规** | 保持；不要引入运行时下载 |
| G12 | `max_instances_per_team_effective=1` | 现有实例生命周期未按"只能 1 个"设计 | 起新实例前先 `stop_instance` |

### 5.3 结构性缺口

| # | 契约要求 | 现状 |
|---|---|---|
| G13 | 智能体要**出题**（`authoring_enabled=true`，3 个窗口，人工审） | **零支持**。且出题要遵守 CTFY-0002（metadata.yaml + compose + 答案投递 + 判分 + 参考解 smoke） |
| G14 | `runs_dir="runs"` 是相对路径，会被解析成 `/ctfy/work/runs` | 结果上反而是好事（会进归档，便于赛后复盘），但必须**显式**确认，不能靠巧合 |

### 5.4 一个反直觉的好消息

`CTFY-0001` §1.2 已经把我们此前的两个关键推断**升格为规范**：① entrypoint 不能用相对路径（我们独立推出过）；② `CTFY_MCP_URL` 不存在、地址只能从 `mcp.json` 或 `CTFY_PLATFORM_URL` 拼（我们也独立推出过）。这说明 `B1 适配层方案`（`docs/b1-adapter-plan.md`）的技术判断是对的，可以直接按其执行，只是现在有了权威出处。

---

## 6. 风险与待确认

| 风险 | 影响 | 建议 |
|---|---|---|
| **赛事规则附件仍为空** | 判罚细则未知（尤其"禁止人工干预"如何认定、出题如何计分） | 报名通过后再查 `/attachments`；同步盯 Discord / QQ 群通知 |
| **`LLM_FLAVOR` 未定** | 若是 `anthropic`，现有客户端完全不可用（+0.5 天） | 注册模型时确认；这是唯一硬阻塞 |
| **177 队 > 150 上限** | 可能加试 11-14，赛程更紧 | 按有加试排计划 |
| **`internet_policy=deny`** | 赛内不能 `pip install`、不能 web 搜索 | 镜像构建期装齐；本仓库 stdlib-only，风险低 |
| **每段 10 小时 + 段末销毁** | 必须支持"中途保存进度、下一段续跑" | 状态落 `/ctfy/work`（会进归档）而非内存 |
| **`max_instances_per_team_effective=1`** | 并发探测不可能，必须串行 | 与 A4 批量基线的串行假设一致 |
| **CTFY-0002 出题复杂度** | 出题是独立工程量（compose + seeder + 参考解 + smoke） | 若出题计分权重高，需单独立项 |

---

## 7. 附录：取证与复现

本报告所有结论可复现，命令如下（均为只读 GET）。

```bash
# 1. 站点是 SPA，html 只有空壳
curl -sS https://ctfy.tsinghua.edu.cn/            # 1.2 KB，<div id="root"></div>

# 2. 平台元信息
curl -sS https://ctfy.tsinghua.edu.cn/api/v1/meta

# 3. 赛事列表 → 拿到 Agent Track 的 id
curl -sS "https://ctfy.tsinghua.edu.cn/api/v1/competitions?limit=50"

# 4. 赛事详情：handbook（规则手册）+ 平台参数
curl -sS https://ctfy.tsinghua.edu.cn/api/v1/competitions/c0d8b4e3dd3b42c095d63d7dfa66431f

# 5. 赛事规则附件（当前为空数组）
curl -sS https://ctfy.tsinghua.edu.cn/api/v1/competitions/c0d8b4e3dd3b42c095d63d7dfa66431f/attachments

# 6. 平台标准：内嵌在前端代码块 registry-*.js 里，无独立 API
#    （/api/v1/standards 与 /api/v1/docs 均 404）
```

本报告留下的取证文件：

| 文件 | 内容 |
|---|---|
| `runs/site/handbook_zh.md` | 赛事手册中文全文（3,046 字） |
| `runs/site/handbook_en.md` | 赛事手册英文全文（6,474 字） |
| `runs/site/agent_track.json` | Agent Track 比赛详情（含全部平台参数） |
| `runs/site/standards/CTFY-0001.md` | **智能体接入协议全文（943 行）** |
| `runs/site/standards/CTFY-0002.md` | 出题包标准全文（1,285 行） |
| `runs/site/registry.js` | 前端标准模块原始代码块（135 KB，两份标准的源文本即内嵌于此） |

> 说明：标准正文由前端代码块 `registry-g6yuny1t.js` 提取（该模块内嵌两份 Markdown 源文，页面渲染时直接使用）。因 Vite 打包时对两份文件用了不同的字符串字面量写法（模板字面量 vs 双引号串），提取脚本做了归一化处理，已用页码/章节结构交叉校验完整性。
