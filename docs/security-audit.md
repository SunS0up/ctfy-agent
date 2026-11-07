# 安全审计报告（仅统计，不修复）

- 审计对象：`E:\ctfy_project` 全部 20 个文件（src/ctfy_agent 10 个模块、tests 3 个、run.cmd、config、docs、.env.example、.gitignore）
- 审计方法：逐文件人工走查 + 危险模式扫描（`shell=True` / `subprocess` / `eval` / `exec` / `pickle` / `os.system` / `urlopen` / `sys.path.insert` / `verify=False` / `CERT_NONE` / `chmod`）+ 既有 13 项 smoke 测试结果交叉验证
- 审计日期：2026-10（框架 v0.2.0）
- 威胁模型：① 比赛规则合规（只许打平台下发实例）；② 运行机本机安全；③ 智能体行为完整性（积分与状态正确）；④ 凭据保护
- **本次未对任何源码做修改**

---

## 0. 统计总表

| 严重度 | 数量 | 编号 |
| --- | --- | --- |
| 高 | 3 | H1 H2 H3 |
| 中 | 3 | M1 M2 M3 |
| 低 | 7 | L1 L2 L3 L5 L6 L7 L8 |
| 信息 | 8 | I1 I2 I3 I4 I5 I6 I7 L4 |
| **合计** | **21** | |

按类别：合规/越权护栏 3（H1 M3 + H2 部分）· 本机代码执行 2（H2 L8 部分）· 提示注入/智能体安全 5（H3 M1 M2 L6 L7 部分）· 凭据与信息泄露 5（L2 L3 L4 I1 I3）· 输入校验 2（L1 L7 部分）· 可用性/健壮性 4（L5 I2 I4 I6）· 其他 2（I5 I7）

---

## 1. 高危（3）

### H1 · `http_request` 跟随重定向，合规护栏可被绕过（SSRF）
- 位置：`src/ctfy_agent/tools_local.py:118-136`（`_check_target` 只校验初始 URL）、`src/ctfy_agent/tools_local.py:173`（`urllib.request.urlopen` 使用默认 opener，未禁用 `HTTPRedirectHandler`）
- 描述：白名单校验只发生在请求发出前，对初始 URL 的 host 做检查。urllib 默认跟随 301/302/303/307/308，**重定向目标不经过任何校验**。
- 攻击场景：题目实例（内容受出题方影响）返回 `302 → http://169.254.169.254/latest/meta-data/`（云主机元数据）或任意外网地址，agent 会跟随请求并把响应体回灌给模型。整个「只允许访问平台下发实例」的合规设计被传输层绕过。
- 现有缓解：无。响应体截断 8000 字符（仅限体积）。

### H2 · `shell` 工具：`shell=True` + 模型全控命令 + 无沙箱
- 位置：`src/ctfy_agent/tools_local.py:197-215`（执行）、`:58-72`（工具规格）、`:157-160`（开关检查）、`config.py:33`（默认 False）
- 描述：开启 `allow_shell` 后，LLM 返回的 `command` 字符串经 `subprocess.run(command, shell=True)` 直接执行：
  1. 以运行 agent 的用户身份在宿主机执行任意命令（Windows 下经 cmd.exe）；
  2. **可读取 `PROJECT_ROOT/.env`**（含 CTFY_TOKEN、DEEPSEEK_API_KEY）；
  3. 可直接用令牌 curl 平台接口，**绕过提交幂等、提交上限、写操作不重试等全部客户端护栏**（这些护栏都在 Python 层，shell 一开全部失效）；
  4. 网络出口完全不受 `http_request` 白名单约束；
  5. `timeout` 参数也由模型控制（超长挂起；负值/非法值引发异常回灌）。
- 现有缓解：默认关闭（`allow_shell=false`），且文档明示"打开需自担沙箱责任"。但 `AGENT_ALLOW_SHELL` 环境变量或 `config/agent.json` 一行即可打开，打开即全失守。

### H3 · 提示注入面：一切平台返回内容未清洗直入 LLM 上下文
- 位置：`src/ctfy_agent/solver.py:291`（题面 description 前 3000 字符）、`:293`（实例信息）、`:295`（历史笔记回放）、`:242-248`（工具结果回灌）；`src/ctfy_agent/platform.py:125-132`（`call_text` 原样渲染）；`src/ctfy_agent/mcp_client.py:308-337`（`render_call_result` / 工具描述进模型上下文）
- 描述：题面、工具输出、实例元数据全部原样拼进对话，没有任何分隔/转义/过滤。SYSTEM_PROMPT 里的四条"硬性规则"（solver.py:24-37）只是软约束。
- 攻击场景（AI 安全赛道出题方向正是这个）：恶意题面诱导模型——
  - 连续提交 3 个垃圾 flag，撞满 `submit_limit=3` 把本题锁死（直接积分攻击）；
  - 反复 `stop_instance` / `start_instance` 制造平台风控；
  - 把已收集的其他题笔记/flag 通过 `http_request`（或 shell，若开）发往题目实例（实例本就受出题方控制）；
  - 拉长循环烧 API 预算（有 `max_steps=12` 封顶，但每步 tool_calls 数量无上限，见 M2）。
- 现有缓解：`http_request` 主机白名单（可被 H1 重定向绕过）、shell 默认关、步数预算。

---

## 2. 中危（3）

### M1 · `looks_accepted` 字符串启发式可假阳性 → 错误锁定题目
- 位置：`src/ctfy_agent/solver.py:40-68`（判定）、`:139-148`（判定后登记）
- 描述：非 JSON 文本走关键词匹配，负向词表（`incorrect/wrong/not correct/failed/rejected/错误/不正确/失败`）**不含 "not accepted"**，而正向词表含 `accepted`——纯文本响应 `"Your flag was not accepted"` 会被判为**通过**。另外 `[工具报错]` 前缀会使 JSON 解析失败落入关键词路径，报错体里出现 `accepted/正确/通过/恭喜` 等词同样误判。
- 后果：状态错标 `submitted`、`flag_value` 记录错误值、该 flag 哈希进 `flags_tried`——**之后真正的 flag 反而被幂等保护拒绝提交**，本题直接丢分且循环提前终止（`reason=flag_accepted`）。
- 现有缓解：JSON 路径判定正确（`{"accepted": false}` 能正确判否）；smoke 测试只覆盖了 JSON 与 "wrong flag" 文本两种情况，未覆盖此假阳性路径。

### M2 · 平台写工具无执行门禁与频控
- 位置：`src/ctfy_agent/solver.py:99-113`（平台工具名命中即直通调用）、`:223-253`（单步 `tool_calls` 数量无上限）；`src/ctfy_agent/platform.py:125-132`（`classify_tool` 仅用于决定重试次数，不做执行闸门）
- 描述：v1 CLI 里 `classify_tool + --allow-write` 的执行闸门在重构为 agent 循环时丢失——现在模型可以在一步内不限次数地调用 `start_instance` / `stop_instance` 及平台上任何其他写工具，无确认、无限速。只有 `submit_flag` 有幂等与上限保护。
- 后果：被注入的模型（见 H3）或失控循环可高频启停实例，触发平台限速/风控（方案文档自己警告过这一点）。
- 现有缓解：`max_steps` 限制总步数（但单步调用数不限）；写工具不重试。

### M3 · 主机白名单结构性宽松：端口限制形同虚设 + 仅名称级校验
- 位置：`src/ctfy_agent/tools_local.py:85-90`（`allow_instance` 同时登记裸主机名与 `host:port`）、`:92-116`（`allow_many` 直接信任平台返回的 host/port/url/endpoint）、`:118-136`（仅按主机名比对，无解析后 IP / 内网段 / 链路本地段过滤）
- 描述：登记实例时裸主机名和 `host:port` 都进白名单，而 `_check_target` 命中裸主机名即放行——**同一主机的任意端口都被允许**，端口号限制是装饰性的。校验只比对字符串主机名，不检查实际解析 IP（无 127.0.0.0/8、10/8、169.254/16 等保留段过滤）；白名单数据源是平台返回值（平台响应被污染即扩白名单）。
- 现有缓解：无端口场景下的确放行是设计使然；但"登记端口却仍放行任意端口"不符合最小权限。

---

## 3. 低危（7）

### L1 · `--run-id` 未清洗，路径穿越
- 位置：`src/ctfy_agent/cli.py:362`（参数定义）→ `src/ctfy_agent/config.py:110-111`（`runs_path / run_id` 直接拼接）→ `src/ctfy_agent/state.py:91-92`（`mkdir(parents=True)` + 写 `state.json`）
- 描述：`--run-id "..\..\x"` 可在 `runs/` 之外创建目录并写文件。当前仅本地用户自伤（参数不来自不可信输入），但属于未做输入校验的真实缺口。

### L2 · `--token` 走命令行
- 位置：`src/ctfy_agent/cli.py:359`
- 描述：令牌以明文出现在命令行，进入 shell 历史（PSReadLine 历史文件）与本机进程列表。帮助文本已自我警示"不推荐"，但功能存在。

### L3 · 部分凭据/明文 flag 输出到控制台
- 位置：`src/ctfy_agent/config.py:123-139`（`masked()` 泄露前 6 + 后 4 字符）、`src/ctfy_agent/cli.py:92-93,126`（doctor 打印）、`src/ctfy_agent/cli.py:335`（demo 的事件 lambda 把含 flag 的完整 payload JSON 打到控制台）、`src/ctfy_agent/solver.py:145`（`flag_accepted` 事件携带明文 flag）
- 描述：约 10 个字符的密钥片段进入终端/日志；demo 模式直接打印 flag（本地演示用途，但会被终端录制/日志收集）。

### L5 · state.json 无并发锁，幂等保护多进程下失效
- 位置：`src/ctfy_agent/state.py:101-109`（原子写但无锁）、`:145-167`（幂等判断基于进程启动时加载的内存态）
- 描述：两个 `solve` 进程跑同一 `run_id` 同一题时都从旧状态出发，同一 flag 会被双方各提交一次——幂等保护恰恰在并发场景失效（原子写只防写坏文件，防不了丢更新）。无文件锁/进程锁。

### L6 · 工具读写分类是词表启发式，误判导致非幂等操作被自动重试
- 位置：`src/ctfy_agent/mcp_client.py:34-42,89-96`（词表）、`src/ctfy_agent/platform.py:127`（判 read → `retries=2`）
- 描述：名称含 READONLY 词且不含 MUTATING 词的平台工具会被判 read 并获得 2 次指数退避自动重试。若平台存在名字不带写关键词、实际有副作用的工具（如 `sync_status`），失败时会自动重复执行。unknown 与 write 均不重试（安全默认正确）。

### L7 · 工具参数无客户端 schema 校验；题目 id 归一化可碰撞
- 位置：`src/ctfy_agent/solver.py:223-233`（模型 args 原样转发）、`src/ctfy_agent/platform.py:122-123`（`call` 不校验 `inputSchema`）、`src/ctfy_agent/cli.py:65-79`（`normalize_challenge` 的 id 回退链 `id→challenge_id→slug→code→key→name→title`）、`src/ctfy_agent/state.py:122-131`（`upsert` 以归一化 id 为键）
- 描述：模型可对平台工具传任意参数（依赖平台侧校验与令牌权限兜底）；两道题若归一化后 id 相同（如都回退到 `name`），state 里会互相覆盖，可能把 B 题的 flag 按 A 题提交。缓解：`_submit` 强制 `challenge_id` 为当前题（solver.py:135-138），跨题提交只会在 id 本身被合并时发生。

### L8 · 端点/开关/解释器完全信任本地配置与环境，无白名单
- 位置：`src/ctfy_agent/config.py:21-50,142-176`（`CTFY_MCP_URL`、`DEEPSEEK_BASE_URL`、`AGENT_ALLOW_SHELL`、`AGENT_RUNS_DIR` 等全部可被 env/config 覆盖）、`src/ctfy_agent/config.py:55-67`（`load_dotenv` 把 `.env` 写入进程环境）、`run.cmd:7-9`（`%DSH_PYTHON%` 决定解释器路径）
- 描述：能写 `.env` / `config/agent.json` / 环境变量的一方可以让 agent 把 Bearer 令牌、完整解题对话（含 flag）发往任意 URL，或直接打开 shell 工具、把 runs 目录指到任意路径。属于"本地写入权限即游戏结束"的信任模型，未做任何钉扎或告警。

---

## 4. 信息级（8）

### I1（=L4 归入信息）· MCP/LLM 客户端跨源重定向会转发 `Authorization` 头
- 位置：`src/ctfy_agent/mcp_client.py:132-138`、`src/ctfy_agent/llm.py:60-63,129-135`
- 描述：urllib 重定向时复制除 content-length/content-type 外的全部请求头（含 `Authorization`）。若平台/模型端点被劫持后返回跨源 30x，令牌/API Key 会随重定向外送。需要端点已被攻破或 DNS 劫持才可触发（TLS 正常校验），故列为信息级。

### I2 · 服务端响应体无大小上限
- 位置：`src/ctfy_agent/mcp_client.py:170`（`response.read()` 全量读入）、`:182-207`（SSE `data:` 行无界累积）、`src/ctfy_agent/llm.py:63`
- 描述：恶意/被攻破的 MCP 或 LLM 端点可用超大响应耗尽内存。`tools_local.http_request` 已做 8000 字节截断（做得对），MCP/LLM 两处没做。

### I3 · 错误消息拼接上游响应体片段
- 位置：`src/ctfy_agent/mcp_client.py:139-154`（截取前 500-1000 字节）、`src/ctfy_agent/llm.py:64-83`、`src/ctfy_agent/cli.py:399-402`（顶层兜底直接打印异常文本）
- 描述：上游回显内容会随错误消息进入本地日志，可能携带敏感字段。

### I4 · 测试用假 MCP 服务的暴露面
- 位置：`tests/fake_mcp_server.py:176-177`（按 Content-Length 无上限读请求体，本机 DoS）、`:179-185`（默认接受任意 Bearer 令牌）、`:239`（`--host` 可绑 `0.0.0.0` 暴露到网络）
- 描述：仅测试组件、默认 127.0.0.1、不含真实数据；若被人手动绑到 0.0.0.0 并留在后台，本机任意进程可调用。

### I5 · 仓库文件硬编码本机用户路径
- 位置：`run.cmd:8`（`C:\Users\Lenovo\...`）、`README.md`、`docs/plan-v1.md` 同款路径
- 描述：共享/公开仓库时泄露本机用户名，且该路径在他机不存在（run.cmd 已有回落逻辑）。

### I6 · notes 无上限增长
- 位置：`src/ctfy_agent/state.py:171-174`（每次追加最多 2000 字符，列表无上限）、`src/ctfy_agent/solver.py:294-295`（最近 5 条回放进 prompt）
- 描述：模型（或注入指令）高频调用 `note` 可使 state.json 持续膨胀（磁盘慢性 DoS），并轻微污染后续上下文。

### I7 · demo 把 tests/ 目录插入 `sys.path`
- 位置：`src/ctfy_agent/cli.py:302-304`、`tests/smoke.py:19-20`
- 描述：`python -m ctfy_agent demo` 从可写目录 `tests/` 导入 `fake_llm`/`fake_mcp_server`。与 `src/` 本身同信任级（能写 tests 就能写 src），仅作提示：不要在多用户可写目录里跑 demo。

---

## 5. 已验证无问题的项（正向清单）

| 检查项 | 结论 | 依据 |
| --- | --- | --- |
| 硬编码密钥 | 无（仅测试假值 `pf_demo_token` 等） | grep 全库；`config.py:97-99,173-174` 凭据只从 env/.env 读 |
| `.env` 进 git | 已排除（`.env`、`.env.*`，保留 `.env.example`） | `.gitignore:1-4` |
| `runs/`（含 flag、转录）进 git | 已排除 | `.gitignore:6-9` |
| 转录文件名路径穿越 | 已防：字符白名单（字母数字与 `-_.`），`..`、`/`、`\`、`:` 均被替换 | `state.py:176-180` |
| state.json 损坏写 | 已防：tmp + `os.replace` 原子写 | `state.py:107-109` |
| `http_request` 协议限制 | 仅 http/https（挡 `file://` 等）；响应截断 8000B；超时 20s | `tools_local.py:122-123,173-174` |
| TLS 证书校验 | 未禁用（全库无 `verify=False` / `CERT_NONE` / 自定义 ssl context） | grep 全库 |
| 危险函数 | 无 `eval` / `exec` / `pickle` / `os.system` / `os.popen`；`shell=True` 仅 1 处（已计 H2） | grep 全库 |
| 跨题提交 | 已防：`_submit` 强制 `challenge_id` 为当前题 | `solver.py:135-138` |
| 测试服务默认绑定 | 127.0.0.1（fake_mcp_server 默认、fake_llm 写死） | `fake_mcp_server.py:239`、`fake_llm.py:59` |
| 提交幂等（单进程） | 有效且被 smoke 实测覆盖（重复提交被拦、不计次） | `state.py:145-167`；`tests/smoke.py` case_solver / case_submit_limit |

---

## 6. 审计结论

1. **最需要先处理的三件事**（按危害排序）：H1（重定向绕过护栏）、H2（shell 全失守面）、M1（"not accepted" 假阳性锁题）。H3 是赛道固有攻击面，只能靠纵深缓解，无法根除。
2. 框架的**凭据卫生与文件安全基本面良好**（无硬编码密钥、gitignore 完整、转录文件名防穿越、原子写、TLS 正常），21 项里没有"密钥直接进仓库/日志全文打印"这类低级问题。
3. 当前所有"硬护栏"都是**客户端自律**性质（白名单、幂等、上限、不重试），在 shell 开启或模型被注入时均可被绕过——比赛正式部署前应把关键约束下沉到网络层/容器层（出口防火墙、容器隔离），而不是继续依赖 Python 层判断。

> 按要求，本报告只统计不修复：以上 21 项均未做任何代码改动，源码保持审计时状态（v0.2.0，smoke 13/13 通过）。
