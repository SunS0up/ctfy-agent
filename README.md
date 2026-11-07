# TPCTF 2026 · 智能体团队赛道 — 解题框架

一个自包含的文件夹，纯 Python 标准库（不装任何依赖），模型用 **DeepSeek V4.1 Flash**。
当前版本是「基本框架」：接口通了、循环跑了、幂等和合规护栏生效，具体解题战术留给后续迭代。

```
E:\ctfy_project\
├─ run.cmd                     # 入口：run.cmd doctor / demo / solve <id>
├─ .env.example                # 令牌 + 模型 Key 模板（复制为 .env）
├─ config\agent.json           # 全部可调参数（模型、预算、开关、tool_map）
├─ src\ctfy_agent\             # 框架本体
│   ├─ config.py               # 配置：agent.json → .env → 环境变量覆盖
│   ├─ mcp_client.py           # MCP Streamable HTTP 客户端（JSON/SSE 双兼容）
│   ├─ httputil.py             # 共享传输助手：同源/白名单重定向护栏 + 响应体限读
│   ├─ platform.py             # 平台封装：工具名自动归类 + schema 转 function calling + 参数校验
│   ├─ llm.py                  # DeepSeek V4.1 Flash 客户端（OpenAI 兼容）
│   ├─ tools_local.py          # 本地工具：http_request / note / shell（默认关）
│   ├─ solver.py               # 主循环：模型 ⇄ 工具，含提交幂等与写操作频控
│   ├─ state.py                # 每题状态机 + 进程间锁 + 原子落盘 + 转录
│   ├─ testing\                # 包内假 MCP + 脚本化 LLM（demo/自测用）
│   └─ cli.py                  # doctor / tools / challenges / solve / state / demo
├─ tests\
│   └─ smoke.py                # 离线自测，18 项
├─ docs\plan-v1.md             # 完整方案：赛制→约束、验收、架构、合规、排期
├─ docs\security-audit.md      # 安全审计报告（21 项，仅统计）
├─ docs\security-fixes.md      # 安全整改记录（审计 → 修复对照）
└─ runs\                       # 运行产物：state.json / transcripts/（gitignore）
```

## 四条命令上手

```powershell
# 1) 配密钥：复制 .env.example 为 .env，填 CTFY_TOKEN 与 DEEPSEEK_API_KEY
Copy-Item .env.example .env ; notepad .env

# 2) 只读自检：MCP 握手 + 工具清单 + 语义动作映射 + 模型配置
.\run.cmd doctor

# 3) 看我是谁、报了哪场比赛（team / competition_id / 赛期）
.\run.cmd whoami
.\run.cmd competitions          # 所有可参与的赛事与练习集

# 4) 拉题单（自动选中你已报名的比赛；也可 --competition <id> 指定练习集）
.\run.cmd challenges
.\run.cmd challenges --competition 4394a10aa5d24f97a1ec3301ca718a45   # CTFARENA 练习集

# 5) 解题：单模型循环，或多智能体协作
.\run.cmd solve CTFARENA-001      # 单个 DeepSeek 跑 12 步
.\run.cmd crew  CTFARENA-001      # 协调者 + web/rev/crypto 专家分工
```

然后开跑：

```powershell
.\run.cmd solve web-01            # 对一道题跑完整循环
.\run.cmd state                   # 看每道题的状态/提交次数
```

没令牌、没 Key 也能先看效果 —— 用假 MCP + 脚本化 LLM 跑通全链路：

```powershell
.\run.cmd demo
```

`run.cmd` 会自动找 Python（本机用 DSH 自带解释器，找不到才回落到 `python`）。

## 模型：DeepSeek V4.1 Flash

| 项 | 值 | 改哪里 |
| --- | --- | --- |
| 模型 | `deepseek-v4.1-flash` | `config/agent.json` 的 `model` 或 `DEEPSEEK_MODEL` |
| 接口 | `https://api.deepseek.com/v1`（OpenAI 兼容 `/chat/completions`） | `llm_base_url` |
| 温度 | `0.2`（解题要稳，不要发散） | `temperature` |
| 单轮上限 | `4096` tokens | `max_tokens` |
| 每题步数 | `12` 步 | `max_steps` / `AGENT_MAX_STEPS` |

换模型只改一行配置；`llm.py` 走的是 OpenAI 兼容协议，任何同协议端点都能接。
费用与用量：`solve` 结束会打印本次 token 消耗，`runs/<run>/transcripts/*.jsonl` 留有完整对话。

## 费用上限（硬刹车）

默认只按 **token** 卡（每道题累计 30 万 token、20 次模型调用）；用量按题落盘，
重跑同一题会**接着算**，不能靠重跑绕过。闸门放在「每次调模型之前」，所以最多
多花一次调用的量。

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `max_tokens_per_challenge` | `300000` | 单题累计 token 上限 |
| `max_llm_calls` | `20` | 单题累计模型调用次数上限 |
| `max_cost` | `0.0` | 单题金额上限（0=不限） |
| `price_input_per_mtok` | `0.0` | 输入 token 单价（每百万），**填了才能卡金额** |
| `price_output_per_mtok` | `0.0` | 输出 token 单价（每百万） |

想卡金额：把单价（每百万 token 价格）和 `max_cost` 都设上，例如
`AGENT_PRICE_INPUT_PER_MTOK=2`、`AGENT_PRICE_OUTPUT_PER_MTOK=6`、`AGENT_MAX_COST=5`
（人民币 5 元封顶，单价按你供应商实际价格填）。`.\run.cmd doctor` 会显示当前上限，
`.\run.cmd state` 每道题多出 `token` 和 `费用` 两列。

## 多智能体协作（crew）

`.\run.cmd crew <challenge_id>` 用一个**协调者**加若干**专家**一起解题：

| 角色 | 职责 | 手里的工具 |
| --- | --- | --- |
| coordinator | 读题、派活、提交、收尾 | `delegate` / `submit` / `note` / `done` |
| web | 抓页面、分析接口/鉴权 | `http_request` + 平台侦察/实例 |
| rev | 逆向 / pwn | `shell` + 附件下载/实例 |
| crypto | 密码学 | `shell` + 平台只读 |
| misc | 兜底 | `http_request` + 平台只读 |

它们**共享同一份笔记、实例与提交状态**，**共享同一份费用预算**（三层刹车跨角色累计）。
分工同时就是权限边界：专家碰不到 `submit_answer`，协调者碰不到 `http_request`/`shell`——
即使某个角色被注入，能做的破坏也被锁在自己那点工具面里。

角色、步数、工具、模型都在 [config/agent.json](config/agent.json) 的 `roles` 里配：
给某个角色加 `"model": "..."` 就能用不同模型（比如 rev 用更贵的推理模型，web 用便宜的 flash）。

## 这套框架刻意做对的几件事

| 机制 | 为什么 | 在哪 |
| --- | --- | --- |
| **工具名自动归类** | 平台真实工具名未知，靠词义把 6 个语义动作映射到真实工具名 | `platform.py` `resolve()` |
| **`tool_map` 兜底** | 自动归类猜错时，在配置里直接写死映射 | `config/agent.json` |
| **提交幂等** | 同一 flag 只交一次，单题上限 3 次，防止扣分/风控 | `state.py` + `solver.py` |
| **写操作不重试** | `submit/start/stop` 失败先查状态，绝不自动重发 | `mcp_client.py` |
| **合规护栏** | `http_request` 只能打平台下发的实例，第三方域名直接拒 | `tools_local.py` |
| **状态机落盘** | 进程崩了能接着跑；非法跳转（如 parsed→submitted）被拒 | `state.py` |
| **转录留存** | 每题一条 JSONL，复盘、申诉、调 prompt 都靠它 | `state.py` |
| **shell 默认关** | 想开必须显式 `allow_shell=true`，并自己保证容器隔离 | `config/agent.json` |

## 自测

```powershell
& "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" tests\smoke.py
```

覆盖 18 项：MCP 握手（JSON/SSE）、401 识别、语义映射、schema→function 转换、读写分类、
主循环、flag 幂等、提交上限、合规拦截（未登记实例/第三方域名）、端点级端口白名单、
重定向护栏、run_id 校验、凭据掩码、状态机、提交结果三态判定。
详细安全整改见 [docs/security-fixes.md](docs/security-fixes.md)。

## 现状与下一步

已经能用：接入、题单、状态、循环、幂等、日志。
还没做（按赛前 3 周排期推进）：

1. **Web 解题器**：常见漏洞链探测（注入、越权、SSRF、上传、模板注入）与 payload 库；
2. **Pwn 解题器**：pwntools 镜像、交互式会话、libc 匹配；
3. **AI 安全解题器**：prompt 注入 / 工具越权 / RAG 投毒的测试台；
4. **调度层**：多题并发、预算分配、失败降级、告警。

待确认（影响打包方式）：平台要求的交付形态（仓库/镜像/托管）、实例与提交额度、
是否允许外部模型 API、有无赛前练习场。详见 [docs/plan-v1.md](docs/plan-v1.md) §9。
