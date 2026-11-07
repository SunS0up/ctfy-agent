# engagement 模式（GAUNTLET 报告制）

CTFy 的 `run_mode` 不止 `instance` 一种（见 `docs/report-ctfy-rules.md:117-127`）。
GAUNTLET 系列用的是 **`engagement`**：没有逐题 flag，得分面只有一份 Markdown 报告，
靶场由平台开好（`task.json` 的 `target_instance` / `target_endpoints`）。

## 为什么必须单独实现

旧版本 `agent_main.py` 的运行模式门只放行 `instance`，遇到 `engagement` 立刻：

```
stop_reason=refused
writeup="unsupported run_mode='engagement'"
```

一次模型调用都不会发出去——平台侧看到的就是「退出码 0、已转发的模型调用 0、agent 没有任何输出」。
这不是 key 过期、也不是方言（flavor）选错，而是本地自检拒绝。

## 交付形态

| 环节 | instance | engagement |
| --- | --- | --- |
| 得分面 | 每题 flag → `POST /api/v1/submissions` | 一份报告 → `PUT /api/v1/competitions/{cid}/challenges/{chid}/report` |
| 结束动作 | `submit_answer` 通过 | `save_report` + `finalize_report` |
| 靶机 | 自己 `POST /instances` | 平台已开好，**不许**再开（`target_instance` 非空） |
| `result.json` | `solved=[qid]`、`answers` 带 flag | `solved=[]`、`fully_solved=false`、`writeup` = 报告正文 |

## 实现（`src/ctfy_agent/engagement.py`）

循环是「侦察 → 记录证据 → 写报告 → save_report → finalize_report」：

1. **工具面**：复用 `LocalTools`（http / http_burst / note / chat / misc_ext），
   把 `task.json` 的 `scope`、`target_endpoints[*].url`、`target_hosts` 全部 `allow_many` 登记；
   再叠加一个本地合成的 `submit_report(report=...)`——它不在 MCP 工具表里，落盘走 `save_report`。
2. **提示面**：system prompt 要求「先摸公开面（robots/sitemap/静态资源/JS 硬编码/HTML 注释/错误页/响应头）、
   由外向内跟、只信实证、`FLAG{...}` 哨兵逐字引用、记录动作而不只记录结论、不打 DoS 不越范围」。
   `note` 工具用来把「可枚举的事实」（版本、数量、命名约定）钉在上下文里——
   工具往返会被压缩折叠，note 不会。
3. **收尾**：`submit_report` 少于 200 字直接拒收并让模型继续查；预算剩 3 次调用时注入
   「预算提醒」逼它收尾；步数用完/LLM 出错则用 `_fallback_report` 交一份带证据的兜底报告
   （宁可短，不能空）。
4. **落盘**：报告正文进 `result.json` 的 `writeup`（截断 6000 字），`solved` 保持空——
   engagement 没有逐题 flag，塞一个假的 `solved` 会让平台记成解题。

## 墙钟预算：比 token 更先耗尽的东西

2026-11-07 第一次带 engagement 的真实测评（v2）暴露了真正的瓶颈：**平台的解题窗口很短**。
实测时间轴：排队 0:02 → 启动靶场 1:14 → Agent 解题 **2:24 后收到 SIGTERM**，
`result.json` 写成 `stop_reason=halted / writeup="signal 15"`，模型调用仍是 0。

也就是说：任何「单次调用可能挂 120 秒」的环节，都会把整轮预算吃光，连一次模型调用都发不出去。
所以 v3 把三个预算钉死：

| 环节 | 旧 | 新 | 理由 |
| --- | --- | --- | --- |
| MCP 建连 / 握手 | `config.timeout`（120s） | `MCP_CONNECT_TIMEOUT = 20.0` | 连不上要快速失败并换下一个候选地址 |
| 模型单次请求 | 120s | `min(config.timeout, 40.0)` | 40s 没首字，这一轮基本废了，留时间给下一轮 |
| 模型调用轮数 | `max_llm_calls`（默认 80） | `min(max_llm_calls, 12)`，可用 `AGENT_ENGAGEMENT_MAX_CALLS` 覆盖 | 12 轮 ≈ 3–4 次侦察 + 1 次收尾 |
| 墙钟硬闸 | 无 | 进程启动后 `AGENT_ENGAGEMENT_DEADLINE`（默认 100s）到点即停并交兜底报告 | 平台约 150s 就 SIGTERM；自己先收尾，至少留下一份可评分报告 |

配套的两处「少做一次往返」：

- MCP 地址不再只试 `mcp.json` 一个：`_mcp_candidates()` 会依次尝试 mcp.json 里的地址和
  `CTFY_PLATFORM_URL + /api/v1/mcp/`，每个 20s；
- engagement 跳过 `_enrich_facets()`（那是给逐题 flag 补 category/tags 的，报告制用不上）。

`stderr` 现在按阶段打点（平台的「Agent 日志」标签能看到）：

```
[mcp] 连接 https://.../api/v1/mcp/（预算 20s）
[mcp] 已连接，工具 97 个
[engagement] start GAUNTLET-LITE-001 max_calls=12 tools=9 scope=['http://initech.example'] ...
[engagement] #1 1.6s tools=2 usage=4158
[engagement] save_report ok
[engagement] finalize_report ok
```

## 本地怎么验（不烧平台测评）

```powershell
$env:LLM_API_KEY = "sk-..."           # 真模型（钱是真的，--turns 就是上限）
python tools\engagement_local.py --turns 10
```

它注入一个假平台（只认 `save_report` / `finalize_report`）+ 真 `DeepSeekLLM`，
跑完打印 `stop_reason`、`turns`、平台收到的调用、以及报告正文开头。
实测（2026-11-07，`deepseek-flash`）：

- `--turns 6` → `save_report` + `finalize_report` 都被调用，`stop_reason=done`，
  `solved=[]`、`fully_solved=false`、`tool_calls_made=13`；
- `--turns 4` → 预算耗尽走兜底，`stop_reason=max_turns`，报告仍然落盘。

## 契约测试

`tools/container_check.py` 的 5.3 用例用的是 `run_mode="qa"`（仍不支持 → `refused`），
`tests/smoke.py` 的 `task_qa.json` 同理，所以放开 `engagement` 不影响既有契约。
`tests/fixtures/task_engagement.json` 是 engagement 的固定夹具（按一次真实 GAUNTLET-LITE-001
的 task.json 抄的，只把 `result.path` 改成 `./result.json`）。
