# 智能体能力盘点：现在能做什么、差什么、什么题型完全不行

**日期**：2026-10-25（只读盘点，未改动源码；所有结论有代码坐标或实测数据）

---

## 0. 一句话结论

**它现在是一个「只会用 HTTP 读页面、会记笔记、会交 flag」的 Web 侦察器**，
外加一层多智能体调度和预算刹车。**Web 类里偏"读+改请求"的题有机会；
凡是需要分析二进制、需要专业工具链、需要 JS 执行、需要 TCP 交互的题，一律为零。**

唯一一次真实解出是"访问首页把 flag 抄下来"的签到题。

---

## 1. 实测基线（唯一硬数据，别用印象替代）

`runs/default/state.json` + 转录：

| 题目 | 类型 | 结果 | 代价 |
|---|---|---|---|
| `CTFARENA-000` | web，**签到题**：访问首页读取 flag | ✅ 解出（`submitted_ok=true`） | 3 步、2 次 `http_request` |
| `CTFARENA-035` | web，弱口令 + OSINT + 社工 | ❌ 两轮 0 flag | 累计约 80 万 token |
| `CTFARENA-001` | — | ❌ `stalled`，0 步 0 尝试 | — |

**50 道题在库，实际动过的只有 3 道，真题材 0 解出。**
A0/A1/A2 三套新机制（题型送达 / 账本去重 / 证据契约）已完成但**至今零真机验证**——
也就是说上表成绩还是"旧版智能体"的成绩，新机制是否有效**未知**。

---

## 2. 能做什么

### 2.1 工具面（全部就这些，没有隐藏能力）

**本地工具只有 4 个**（`tools_local.py` 的 4 个 SPEC）：
`http_request`、`shell`、`note`、`report`。

| 工具 | 真实能力 | 关键限制 |
|---|---|---|
| `http_request` | GET/POST/PUT/PATCH/DELETE/HEAD、自定义 header、body；**`grep` 正则在 2MB 全响应上搜、只回 ±80 字符片段**；`offset` 分页 | 5000 字符窗口；**不能执行 JS**；无 cookie jar（要模型自己带）；重定向跨源被拦 |
| `shell` | 任意命令，`cwd` 锁在 `runs/<run>/sandbox/<cid>/`，超时上限 120s | **`allow_shell` 默认 false**；misc 角色即使开了也没有；`shell=True` **无命令白名单** |
| `note` | 写本地笔记（不回传协调者） | 只是备忘录 |
| `report` | 专家→协调者的唯一结论出口，**必填逐字 `evidence`** | 最多 2 次拒绝，超限打 `[无逐字证据·未验证]` |

**平台工具 26 个**（`platform.py` 的 `MODEL_TOOLS`）：读题、列附件、启停实例、流量、
提交、榜单、提示（`list_hints`/`view_hint`）。**没有**任何出题工具被放行。

**没有文件读写工具**——`read_file`/`write_file`/`xxd` 一个都没有，模型只能通过 `shell` 间接碰文件。

### 2.2 编排与刹车（这部分是相对成熟的）

- 五角色：`coordinator` + `web` / `rev` / `crypto` / `misc`，delegate 契约是
  `role + objective + done_when`。
- 三层预算：单题 token（500k）、LLM 调用数（80）、步数（12，专家 8–12）。
- 确定性账本去重（含归一化 args）+ 停滞检测（只认账本新增尝试）+ 输出证据校验。
- 实例操作频控 5 次、提示 50 次、**单题提交上限 3 次**（`submit_limit=3`）。

---

## 3. 差什么功能（按"不做就零分"排序）

### P0 — 致命，不做直接 0 分

| 缺口 | 现状 | 后果 |
|---|---|---|
| **容器适配层（B1）** | `Dockerfile` 无 `ENTRYPOINT`、`WORKDIR /ctfy`；无 `task.json` 解析、无 `result.json` 写出 | 现在提交镜像 = 平台读不到 rollup = **0 分**，与解题能力无关 |
| **模型接入** | `llm.py` 只有 OpenAI `/chat/completions`；`config.py:227` 只读 `DEEPSEEK_API_KEY`，不认 `LLM_API_KEY` | 若赛道注册的是 anthropic flavor，镜像直接不可用 |
| **A0/A1/A2 真机验证** | 全部只有离线证据 | 不知道新机制是否真的改变行为 |

### P1 — 硬缺口，决定"真题材能不能碰"

| 缺口 | 证据 | 影响 |
|---|---|---|
| **无文件落盘工具** | 本地工具只有 4 个；`download_challenge_attachment` 直接 `platform.call_text` 进上下文 | 附件以 **base64 内联**进 16k 上下文窗口，稍大的二进制直接爆窗 |
| **上下文窗口太小** | `max_context_tokens = 16000`（`config.py:41`） | 读一个 200KB 的 JS 就满，后面全靠滑窗丢历史 |
| **镜像工具链缺失** | `Dockerfile` 只装 `curl grep coreutils ca-certificates` | **无** `file`/`strings`/`objdump`/`gdb`/`nc`/`unzip`/`binwalk`/`zsteg` |
| **无 pwn 角色** | `crew.py:138` 的 delegate enum 只有 `web/rev/crypto/misc` | Pwn 题只能硬塞给别的角色，而它们没有对应工具 |
| **misc 角色没 shell** | `config/agent.json`：`misc.allow_shell=false`，本地只有 `http_request` | 杂项题（取证/隐写）**结构上做不了** |
| **无 TCP/二进制交互** | 无 `nc`/`socat`/`pwntools`，只有 HTTP 工具 | Pwn、非 HTTP 服务一律无解 |
| **输出上限 4096** | `max_tokens=4096`（rev/crypto 覆盖 8192） | 长脚本/长分析会被截断 |

### P2 — 提升项

- **无 JS 执行**（无浏览器/无 node）：SPA 题、前端加密、需要跑 JS 的 Web 题做不了。
  035 是 Vue3 SPA，模型只能看到 215B 兜底页——A0 补了 tags，但**这条能力缺口没补**。
- `shell` 无命令白名单（已知项，等 B3 容器隔离）。
- 无并发、无字典/模糊测试工具（无 sqlmap/ffuf/gobuster）。

---

## 4. 题型能力矩阵

| 题型 | 判定 | 依据 | 具体卡点 |
|---|---|---|---|
| **Web（读页面/找接口/改请求）** | ⚠️ **有机会** | `http_request` 支持方法/头/体/正则搜；000 已解出 | 无 JS 执行、无浏览器、无并发；16k 上下文读大 JS 吃力 |
| **Web（注入类）** | ⚠️ 弱机会 | 能手搓 payload | 无 sqlmap、无自动化盲注、提交限 3 次、步数仅 12 |
| **Web（前端加密/SPA 路由）** | ❌ **基本不行** | 无 JS 引擎 | 035 实证：只看到兜底页 |
| **Crypto（古典/编码/小参数 RSA）** | ⚠️ **小规模可以** | `shell` 可跑 python 大整数 | 镜像**无** `pycryptodome`/`gmpy2`/`sympy`；格运算、DLP、大数分解不行 |
| **Crypto（附件型）** | ❌ 难 | 附件 base64 进上下文 | 16k 窗口 + 无落盘工具 |
| **Reverse** | ❌ **基本不行** | 无 `file`/`strings`/`objdump`/`gdb` | 只能读 base64 文本；稍大二进制爆窗 |
| **Pwn** | ❌ **完全不行** | 无角色、无 `nc`/`socat`/`pwntools`/`gdb` | 三重缺失，不是调优能解决的 |
| **Misc（取证/隐写/压缩包）** | ❌ **完全不行** | misc 无 shell；镜像无 `binwalk`/`zsteg`/`unzip` | 结构 + 工具双重缺失 |
| **Misc（纯文本/编码）** | ⚠️ 可以 | `http_request` 的 grep + 文本处理 | 仅限不需要外部工具的部分 |
| **AI 安全（HTTP API 型）** | ⚠️ **唯一可行路径** | prompt injection / 越权走 `http_request` 即可 | 无专用角色，但 Web 角色能覆盖 |

**归纳**：真正能打的只有一列——**「HTTP 可达 + 不跑 JS + 不需要外部二进制工具」**。
其他全部落在"结构上做不到"，不是"提示词没写好"。

---

## 5. 结论与建议顺序

1. **先补 P0（B1 适配层）**。现在解题能力再强也是 0 分，因为平台读不到 `result.json`。
2. **再补 P1 的「文件落盘 + 镜像工具链」**。这两项一次改动就能把
   Crypto / Rev / Misc 从"不可能"抬到"有机会"，性价比最高。
3. **Pwn 单独评估**：需要角色 + TCP 工具 + 二进制工具链，是独立工作量，
   建议先明确赛题里 Pwn 占比再投入。
4. **Web 只补 JS 执行（可选）**：收益集中在 SPA/前端加密题，优先级低于上面两项。
5. **别忘了 A0/A1/A2 从没真机跑过**——建议在补完 P1 后先单跑 1 道 easy 验证，
   再谈批量。

> 一句话：**当前不是"模型不够聪明"，是"工具面和镜像根本不允许它做那些题"。**
> 先把手装上，再谈脑。
