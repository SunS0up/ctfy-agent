# 实测记录：真实平台接入结果

记录日期：2026-10（系统时钟）
环境：Windows / Python 3.12 / 零第三方依赖
结论：**框架已真实接入 `ctfy 3.4.6`，并完成一次真实提交与平台侧记账确认。**

---

## 1. 端点

| 项 | 值 |
| --- | --- |
| MCP 地址 | `https://ctfy.tsinghua.edu.cn/api/v1/mcp/` |
| 协议 | `2025-06-18`（Streamable HTTP，无状态模式） |
| 服务端 | `ctfy 3.4.6`（FastMCP） |
| 鉴权 | `Authorization: Bearer <token>` |
| 工具数 | **97** |

⚠️ 关键坑：`/v1/mcp/` 是前端单页应用的通配路由。
`GET` 返回 200 `text/html`，`POST` 返回 `405 {"detail":"Method Not Allowed"}` —— 看起来像服务端拒绝，
其实是**路径写错了**。正确的挂载点是 `/api/v1/mcp/`（带 `api/`），路径必须精确到末尾斜杠。

## 2. 真实 API 是「实例中心」，不是「题目中心」

原设计假设 `submit_flag(challenge_id, flag)`，实测完全不是：

| 动作 | 真实工具 | 关键参数 |
| --- | --- | --- |
| 拉题单 | `list_competition_challenges` | **`competition_id` 必填** |
| 起环境 | `start_instance` | `challenge_id`、`competition_id` |
| 停环境 | `stop_instance` | **`instance_id`**（UUID，不是 challenge_id） |
| 判题探针 | `verify_answer` | `instance_id`、`answer`、`question_id` |
| **真提交** | **`submit_answer`** | `instance_id`、`answer`、`question_id` |
| 排行榜 | `get_scoreboard` | —— |

`start_instance` 的真实返回：

```json
{
  "id": "19c999fa-c133-4386-9563-ed72c69656ca",
  "surface": {
    "services": [
      {"service_type": "https", "host": "snohkp6l53wskwuwv.node4.ctfy.run",
       "port": 443, "path": "/", "label": "app"}
    ]
  }
}
```

实例地址藏在 `surface.services[]` 里（早期版本只认顶层 `host/port`，会漏登记 → 已在
`LocalTools.allow_many` 改成递归提取）。

## 3. 免费 oracle：`verify_answer` 不写提交记录

这是实测最有价值的一条：
`verify_answer` 与 `submit_answer` 用同一个判题器，但 **不产生提交记录**。
所以正确姿势是「先探针、再提交」：

```
verify_answer(instance_id, answer)  ->  false   # 未通过，不消耗次数，继续换思路
verify_answer(instance_id, answer)  ->  true    # 通过，再 submit_answer 记账
```

`config.oracle_limit`（默认 50）兜住探针次数，避免变成暴力枚举。

返回值是**裸布尔**：`true` / `false`（`structuredContent` 里是 `{"result": true}`）。
`looks_accepted` 已支持裸布尔；早期版本只认对象，会把 `true` 判成「无法判定」。

## 4. 工具面收敛：97 → 26

97 个工具里有大量**不该给模型碰**的：

| 工具 | 风险 |
| --- | --- |
| `submit_registration` | **整体替换**报名档案 |
| `register_for_competition` | 报名/退赛等不可逆状态 |
| `join_awd_arena` / `leave_awd_arena` | 改动 WireGuard 网络接入 |
| `upload_authored_challenge` / `revise_*` / `finalize_report` | 出题与报告流程 |
| `contact_organisers` / `reply_to_message` | 以队伍名义对外发消息 |

`platform.MODEL_TOOLS` 只放行 26 个解题必需项；`solver._platform_names()` 也只认这个集合，
即使模型幻觉出 `upload_*` / `register_*` 也执行不了。离线假服务（无 `get_me`/`submit_answer`）自动全放开，
自测不受影响。

**例外：提示类工具（A0 附带项）**。`list_hints`（免费列层级，需 `challenge_id`+`competition_id`，
返回 `tiers`）与 `view_hint`（**扣费、按队永久计费、不可回滚**）现已放行，否则有 hints 的题
agent 结构性拿不到。护栏：只由模型显式发起，代码里**没有任何自动调用**；
`state.tool_counts` 的写操作频控目前**未覆盖** `view_hint`（未获批，勿擅自加）。

## 5. 真实验证清单

| 环节 | 结果 |
| --- | --- |
| MCP 握手 + tools/list | ✅ 97 工具 |
| `get_me` | ✅ 用户 `yzqcyl`，队伍 `123321`（captain） |
| 参赛状态 | ✅ TPCTF 2026 · Agent Track，`upcoming`，2026-11-15 → 11-22，170 队 |
| 拉题单 | ✅ 正式赛 0 题（未开赛）；练习集 `CTFARENA` 返回 50 题 |
| `start_instance` | ✅ 返回 instance_id + https 服务地址 |
| 合规护栏 | ✅ 登记 `host:443`；访问 `example.com` 被拒 |
| `http_request` | ✅ 200，页面内取到 `FLAG{...}` |
| `verify_answer` | ✅ 正确→`true`，错误→`false` |
| `submit_answer` | ✅ 平台确认通过 |
| `stop_instance` | ✅ 正常停止；解题后平台亦自动销毁实例 |
| 平台侧记账 | ✅ `get_my_solves` 出现 `CTFARENA-000`，`ranked: true` |

一次真实 LLM 解题（`DeepSeek-V4.1-Flash-1017`）：

```
[step 1] 我先获取目标实例的主页。            -> http_request
[step 2]                                    -> http_request (带 :443)
[step 3]                                    -> submit_answer  [!!] 平台确认通过
steps 3 / attempts 1 / tokens 23817 / reason flag_accepted
```

## 6. 环境要点

- 并行智算云 Key 只能配 `https://ai.paratera.com/v1`，**不能**用 `api.deepseek.com`；
  模型名实测为 `DeepSeek-V4.1-Flash-1017`（另有 `DeepSeek-V4.1-Flash`）。
- 平台限制 `max_instances_per_team_effective: 1` —— 一次只能开一个实例，收尾务必 stop。
- 正式赛 `internet_policy: deny`（实例出网被禁），`tcp_access: tls_sni`。
- 开赛前用练习集（`CTFARENA` / `UPLOADLABS`，`agents_allowed: true`）压测。
