# 出题（authoring）能力空白核查 + 外包交接方案（2026-10-25）

> 只读核查，未改动任何源码。所有数字都有命令输出或源码/标准行号支撑。

## 1. 核查结论

| 能力 | 代码现状 | 判定 |
|---|---|---|
| **出题 / authoring** | `grep -rni "authoring\|challenge_package\|deliverable" src/ tests/ config/` → **0 命中**；`cli.py` 9 个子命令（doctor/tools/whoami/competitions/challenges/state/solve/crew/auto/demo）**无一个与出题有关** | **完全空白**（设计、代码、测试、文档全无） |
| B1 容器适配层 | 无 `agent_main.py`；`Dockerfile` 26 行、无 `ENTRYPOINT`、`WORKDIR /ctfy`；无 task.json/result.json 读写 | **代码空白，但方案完备**（`docs/b1-adapter-plan.md`，已审核可行） |
| qa / defence / awd / engagement 四种 run_mode | 无实现 | **故意空白**（方案里就是拒跑，避免烧钱） |
| 解题侧 A0/A1/A2 | 已实现，47/47 单测绿 | 已有，但**无真实 run 验证** |

**结论：真正"完全空白"且正赛必需的是出题。** 平台侧不是没有接口——`docs/mcp-tools.json` 的 97 个工具里有 **23 个出题专用工具**（`get_authoring_template`、`check_authored_challenge`、`upload_authored_challenge`、smoke 日志、配额、拒稿暂停、贡献者协议……），规则写在 CTFY-0002《The ctfy challenge package》（1285 行）。我们一行都没接。

## 2. 外包前必须先想清楚的 5 件事

1. ~~**出题是计分的，但也是双刃剑。**~~ **（2026-10-25 二次核查后更正，见 `docs/why-authoring.md`）**
   本赛事 `scoring_rule` 为空 → 走默认 `flag_count`，榜单排序为 `flags_solved → solved → last_solve_at`，
   **出题不加分**；`authoring_reception` 对非 `peer_authoring` 规则直接返回 `scored=False`
   （`server/authoring_reception.py:63-72`），解自己出的题也不记 solve 行。
   只有主办方把规则改成 `peer_authoring` 且 `authoring_weight > 0` 时才计分（那时按
   `PeakedCurve`，解出率 5%–25% 满分）。
   **所以外包出题的理由是"合规 + 单项奖 + 能力展示"，不是"加分"**——预算规模应按前者定，不要按后者定。
2. **窗口极窄。** 正赛 `authoring_enabled=true`，但只在 **11/21 的三个 2 小时窗口**开放上传；审核是人工的，被拒还可能触发 `get_authoring_pause`（按 competition 统计拒稿数）暂停上传。**试错机会不是无限的。**
3. **上传必须用队伍 token，不能给外包商。** `CTFY_TOKEN` 是队伍级凭据（能提交 flag、能改状态），有效期 30 天。给出去等于交出账号。
4. **我们本机没有 Docker。** web 题必须有 `docker-compose.yml` + `services/<name>/Dockerfile` + `solution/exploit.py`，要本地起服务验证——**外包商必须有 Docker，这是筛选硬条件**。
5. **交付物不是"一段代码"，是"能过审的题"。** 判断标准只有一个：`ctfy authoring check` 0 error + 平台 smoke 通过。

## 3. 建议拆成两包，别一次包出去

| 包 | 内容 | 工期估计 | 风险 | 建议 |
|---|---|---|---|---|
| **P1 出题包（先做）** | 人工/半自动产出 **1–2 道 web 题**的完整 package，本地 `ctfy authoring check` 0 error，附部署与解题说明 | 3–5 天 | 低（可完全离线验收） | ✅ 先包这个，验证流程与质量 |
| **P2 出题 agent** | 在我们的仓库里新增 `authoring.py` + `cli.py author` 子命令，串起"取模板 → 生成 → 本地 check → 上传 → 读 smoke 日志 → 按拒稿意见修订"闭环 | 5–8 天 | 高（依赖平台、要 token、窗口受限） | ⏸ 等 P1 跑通、且你愿意给受限 token 或让我们代跑上传 |

P1 拿到手之后，P2 才有意义——否则外包商在不知道"什么算过审"的情况下写自动化，大概率产出一堆 check 不通过的包。

## 4. 注意事项（按风险排序）

### 4.1 凭据与账号
- **绝不给** `.env`（`CTFY_TOKEN` / `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`）。
- **绝不把平台 MCP 端点 + 队伍 token 一起给**。若必须让外包商直连，另开一个**一次性、可吊销**的令牌，且只在 11/21 窗口内启用，事后立即吊销。
- 推荐分工：**外包商只交 `.tar.gz` 包 + check 输出，上传与 smoke 由我们执行，把平台返回的 JSON 原样回传给他们迭代。** 这样零凭据外泄。

### 4.2 合规字段（填错会被拒或引发争议）
`upload_authored_challenge` 必带：`license`、`provenance`、`source_url`、`ai_assisted`、`eval_use`、`accepted_terms_version`、`credit`、`note`。
- `ai_assisted` 必须如实填（人机协作是允许的，隐瞒才是问题）。
- `accepted_terms_version` 来自 `get_authoring_agreement`——**需要队长先签贡献者协议**，否则上传门都进不去。
- 用第三方代码/镜像必须在 `provenance` / `source_url` 写清来源。

### 4.3 答案纪律（最容易翻车）
CTFY-0002 §4.6：`services/` 下**不能出现字面量 `FLAG{…}`**；Dockerfile 不能 bake 答案；build arg 不能叫 `ANSWER_*`。flag 由 `flag-rotator` 注入，`seeder` 负责 per-team 值。
→ 合同里直接写：**"任何答案不得出现在源码、镜像层、attachments、prompt 里，只能由 rotator/seeder 注入"**，并作为验收项。

### 4.4 包结构硬约束（返工高发区）
- 根目录必须有 `metadata.yaml`；web 必须有 `docker-compose.yml`；`build:` 的服务必须有 `services/<name>/Dockerfile`；必须有 `solution/exploit.py` 或 `solution/Dockerfile`。
- **不允许符号链接/硬链接/设备文件**（`archive_member_not_a_file`），路径不许绝对、不许 `..`、不许反斜杠/NUL。
- 体积：**包 64 MiB / 解包 128 MiB / 单文件 64 MiB / ≤2000 个文件**（平台 `/api/v1/meta` 的 `authored_archive` 与 §7.4 一致）；`attachments/` 最多 2000 文件、16 层深。
- `metadata.yaml` 必填：`name`、`description`、`difficulty`（easy|medium|hard）、`tags`（非空字符串列表）；`category` 默认 `web`；题目 id 必须匹配 `^[a-z][a-z0-9_]*$`。

### 4.5 运行环境
- 比赛期 `internet_policy=deny`：**题面服务不得依赖任何外网资源**（CDN、公网 API、外部字体全不行），所有依赖打进镜像。
- 实例 TTL 默认/最大都是 **10800s（3 小时）**，题要在这之内可被解出。
- web 模板栈只有 5 种：`go`、`python-flask`、`node-express`、`ruby-sinatra`、`php-apache`。

### 4.6 排期
- **11/21 才开上传窗口（三个 2h）**，人工审核有延迟，拒稿还可能暂停。
- 因此：**要求 11/10 前交 P1**，留出两周做本地复核与两轮修订；不要压到窗口当天。

### 4.7 知识产权
- 合同写明：源码与题目版权归队伍；外包商不得复用/公开；交付含全部构建文件与说明；签 NDA。
- 交付必须**自包含**：不引用外包商私有仓库、私有镜像、需要联网拉取的资源。

## 5. 交接文件清单

### ✅ 可以给（都是公开标准或我们自己的提炼件）
| 文件 | 用途 |
|---|---|
| `runs/site/standards/CTFY-0002.md` | **出题包唯一权威标准**（1285 行），必给 |
| `runs/site/standards/CTFY-0001.md` | 若同时做 P2（agent 协议），必给 |
| `runs/site/handbook_zh.md` | 赛事手册（出题方向、评审口径） |
| `docs/report-ctfy-rules.md` | 我们提炼的赛事参数与硬约束 |
| `docs/mcp-tools.json` | 97 个工具 schema（出题 23 个），**已确认无凭据** |
| `.tools/ctfy-src/` 或 `https://github.com/wangyihang/ctfy` | 官方 CLI 与 audit 实现（`ctfy/cli/authoring.py`、`ctfy/challenge/audit.py`、`scaffold.py`） |
| `https://github.com/CTF-Arena/challenges` | **2848 道参考题 / 20 个 suite**，让外包商照抄结构最快的路径 |

### ❌ 不给
`.env`（全部密钥）、`runs/default/transcripts/*.jsonl` 与 `runs/default/state.json`（含队伍身份、平台返回、可能含 flag 痕迹）、`config/agent.json`（内部模型策略）、`src/` 解题代码（P1 不需要，给了只会增加泄露面）。

## 6. 参数表（原样抄给外包商）

```yaml
# 赛事
competition_id: c0d8b4e3dd3b42c095d63d7dfa66431f
platform: https://ctfy.tsinghua.edu.cn        # 只读公开信息用；上传由我方执行
platform_version: 0.7.0

# 出题目标（P1）
category: web
stack: python-flask            # go | python-flask | node-express | ruby-sinatra | php-apache
difficulty: easy | medium | hard
slug: <目录名，小写短横线>      # 上传后平台 mint AUTHORED-<8hex>，slug 仅用于替换待审版本

# metadata.yaml 必填
name: <题名>
description: <题面，可含 {{ param.port }}>
difficulty: <同上>
tags: [<非空字符串列表>]
questions:
  - id: flag                  # ^[a-z][a-z0-9_]*$
    prompt: <提问>
    format: flag              # flag | raw
    # 动态题：不写 answer（平台每队现造），由 flag-rotator 注入

# 上传参数（由我方在窗口内执行）
upload_authored_challenge:
  competition_id: c0d8b4e3dd3b42c095d63d7dfa66431f
  slug: <同上>
  archive_base64: <tar.gz 的 base64；远程 MCP 必须用这个，不用 path>
  license: <许可证，如 MIT / CC-BY-4.0 / proprietary>
  provenance: <原创 / 改编自某题，写清>
  source_url: <来源链接，原创可空>
  ai_assisted: true | false
  eval_use: true                 # 是否允许平台用于评测
  accepted_terms_version: <get_authoring_agreement 返回值>
  credit: <署名>
  note: <给审核人的说明>

# 限制
archive: 64 MiB / unpacked: 128 MiB / 单文件: 64 MiB / 文件数: 2000
attachments: ≤2000 文件、≤16 层
instance_ttl: 10800s（3h）
```

## 7. 验收标准（写进合同，逐条可执行）

1. `ctfy authoring check ./<pkg>` → **0 error**（本地审计，无需 token；命令见 CTFY-0002 §1 开头）。
2. `docker compose up` 能起，`entry_urls` 可访问，服务健康检查通过。
3. `solution/exploit.py` 按契约可跑：入参 `<host> <port>` 或 `CTFY_TARGET_HOST/PORT`，stdout 每行 `Q[<id>]: <value>`，退出码 0/1/2/3/4 语义正确（§6.2）。
4. 我方执行 `check_authored_challenge`（平台 dry run）→ 无 refusal；`upload_authored_challenge` 后 smoke 通过。
5. 源码/镜像/attachments/prompt 中**不含任何答案字面量**（`FLAG{`、`ANSWER_*` 自查 + 人工复核）。
6. 交付含：包目录、构建脚本、`check` 输出原文、部署说明、预期解法与难度自评。
7. 若做 P2（agent 代码）：纯 stdlib、不加第三方依赖、不改 `solver.py`/`crew.py` 既有行为、`tests/smoke.py` 保持 47/47 全绿。

## 8. 一句话建议

**先包 P1（1–2 道 web 题，离线验收，零凭据），别急着包 P2。** P1 跑通后你会同时得到三样东西：一道能计分的题、一套可复用的目录模板、一份"什么算过审"的真实反馈——再拿这些去约束 P2，成本和风险都会低一个量级。
