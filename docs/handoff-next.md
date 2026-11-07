# 接盘文件（TPCTF 2026 智能体赛道）— 新对话从这里开始

> 给接盘 agent：**先通读本文件再动手**。这是当前会话的完整上下文快照，2026-10-25 暂停。
> 暂停点：**「功能」阶段**，容器适配层（B1）明确放最后。

---

## 0. 一句话现状

项目在 `E:\ctfy_project`。交付物是**容器镜像**（纯 stdlib Python 3.12，读 `/ctfy/work/task.json` → 跑 Crew → 写 `/ctfy/work/result.json`）。
解题能力已经**真机跑通并解出过一道题**（CTFARENA-006）；`http_request` 的 `diff` 参数**已落地**（smoke 55/55）；
**下一步 = 功能增强的剩余项（method_cards 扩充需等轨迹日志），或直接推 B1 容器适配层**。

---

## 1. 硬约束（必须遵守）

1. **零 token**：写代码/测试阶段不调真实 LLM，不跑 Crew 端到端（花真钱/真配额）。smoke 用 `ScriptedLLM`，全离线。
2. **禁止改动**（未再获批准前）：`platform.py`、`state.py`、`cli.py`、`llm.py`、`ledger.py`。
   - `solver.py` 已获批准改动过（A3 方法卡注入 + usage 落盘 + competition_id 兜底），可继续改。
3. 可改：`solver.py`、`crew.py`、`tools_local.py`、`method_cards.py`、`config/agent.json`、`tests/smoke.py`、`docs/*`、`PROGRESS.md`。
4. 真机跑题前必须问用户（花钱）；跑完要停实例（配额 1）。

---

## 2. 环境（PowerShell，从 `E:\ctfy_project` 跑）

```powershell
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = "E:\ctfy_project\src"     # 跑 python -m ctfy_agent 必须
# 自测（应 55/55 绿）
& $py tests\smoke.py
# 编译
& $py -m compileall -q src tests
```

真机跑题（**注意：`--config`/`--run-id` 是全局参数，必须在子命令 `crew` 之前**，否则 argparse 报错）：

```powershell
& $py -u -m ctfy_agent --config E:\ctfy_project\runs\_probe_config.json --run-id probeN crew CTFARENA-006
```

---

## 3. 仓库地图（关键文件）

| 文件 | 作用 |
|---|---|
| `src/ctfy_agent/crew.py` | Crew 编排：协调者(4 工具 delegate/submit/note/done) + 专家(web/rev/crypto/misc)。`PLATFORM_SCOPE`、`DEFAULT_ROLES` |
| `src/ctfy_agent/solver.py` | 单 agent solve + `run_role`(专家有界对话)。`_compact_history` 滑窗、`_competition_id` 兜底、`_autofill` |
| `src/ctfy_agent/tools_local.py` | 本地工具：`http_request`(grep/offset/limit/5000窗口/body缓存/Range拦截/cookie jar)、`shell`、`note`、`report` |
| `src/ctfy_agent/method_cards.py` | A3 题型方法卡：23 张卡 / 74 键，按平台 tag 词表索引，web 门控注入 |
| `src/ctfy_agent/ledger.py` | A1 确定性账本（去重/证伪） |
| `src/ctfy_agent/state.py` | Store/Challenge（**禁改**） |
| `src/ctfy_agent/platform.py` | MCP 客户端（**禁改**） |
| `src/ctfy_agent/config.py` | 配置加载 + env 映射 |
| `config/agent.json` | 角色/模型/预算。web 平台面已裁到 9 工具白名单 |
| `tests/smoke.py` | 全离线回归（55 例），`ScriptedLLM` + 本地 HTTP server 脚手架 |
| `docs/borrowed-techniques.md` | 可借鉴数据 + 本轮落地清单 |
| `docs/review-token-analysis.md` | token 分析 + 首次解出记录 |
| `PROGRESS.md` §9 | 本轮进度存档 |

---

## 4. 已完成（本会话，smoke 55/55 已验证）

1. **裁工具 28→13**：web 平台面 9 工具白名单（`config/agent.json` + `crew.py::DEFAULT_ROLES`），schema 9,205→3,624 est（−61%）。
2. **逐调用 usage 落盘**：`solver.solve`/`solver.run_role`/`crew` 协调者三处 transcript 记录加 `"usage"`（真机已验证，含 cached_tokens / reasoning_tokens）。
3. **competition_id membership 兜底**（P1）：`solver._competition_id(challenge_id)` 按「题目属于哪场」反查 + 写回 meta 缓存。修掉 `start_instance` 报 `competition_required`（crew 路径 + 新 run 目录 meta 为空必踩）。
4. **http_request 会话 cookie jar**：`Set-Cookie` 自动回传 + 显式 Cookie 优先 + 缓存 key 含 cookie（`(netloc, path, cookie)`）。
5. **预算/上下文**：`max_tokens_per_challenge` 500000→**800000**；新增 `max_context_tokens` **32000**（原 16000）。
6. **`http_request` 的 `diff` 参数**（见 §6，已完成）。

---

## 5. 真机实测事实（本轮）

- 网关 `https://ai.paratera.com/v1`：85 模型可用；角色模型 **Kimi-K2.7(coordinator/web)、Kimi-K3(rev)、DeepSeek-V4-Pro-0813(crypto)、GLM-Z1-AirX(misc)** 全在；全局模型 DeepSeek-V4.1-Flash-1017。
- 平台：ctfy 3.4.6，token 有效。练习场 **CTFARENA**（competition_id=`4394a10aa5d24f97a1ec3301ca718a45`，50 题），正赛是「TPCTF 2026 · Agent Track」（upcoming，id 不同）。
- **006 解出**（`submitted`/`flag_accepted`）：430,873 token / 56 调用 / 7 回合 / ~11min；路径 SSRF→`file://` LFI→shell 跑 Python 批量读→flag；web prompt 占 92%，cached_tokens 90%。
- 001 stalled 80,018/7calls、035 stalled 504,953/50calls（旧，A1-A3 之前）。
- 003 跑到 135,464/23calls 被环境杀掉（会话切换，非失败），实例已清理。
- 单次 prompt 中位 7,739（035 时代 9,980）；最高 15,229 → 16k 滑窗丢证据是真实瓶颈，故上调到 32k。

---

## 6. 已完成：`http_request` 的 `diff` 参数（2026-10-25 落地，smoke 55/55）

> 状态：**已实现并测过（零 token，全离线）**。真机未验证——IDOR 类题跑一次就能确认收益。

**契约**：`diff: bool`（`as_bool` 宽松解析，模型写 `"true"`/`"1"` 也认），`diff=True` 时：

1. **绕过缓存读**（`if hit is not None and not diff`），强制重抓当前 URL；
2. 基线 = 缓存里**同 netloc + 同 path（忽略 query）最近一条**的 payload（`_diff_baseline`，倒扫 dict 插入序）；
   **基线必须在 `_cache_put` 之前取**，否则刚抓的这份会变成「最近一条」自己和自己比；
3. `difflib.unified_diff(n=1)` 行级比对，只回差异行，截断 `DIFF_MAX_CHARS=4000`（`added_lines`/`removed_lines`
   仍是全量计数，只有展示被截）；
4. 输出 `{"status", "headers", "diff", "added_lines", "removed_lines", "baseline_had", "truncated"}`（+`total_bytes`）；
   无基线 → 退回普通窗口输出 + `baseline_had=false` + 一句提示（**不假装比过**），逐行相同 → `diff: ""` + note；
5. 结果照常 `_cache_put`，枚举时基线链条自动前进。

**配套改动**：`solver._ledger_gate` 对 `diff=true` 放行 —— `ledger.normalize_args` 只保留
method/url/body/grep/offset/limit（**ledger.py 禁改**），不看 diff，否则「同 URL 先普通读、再 diff」
会被当作「完全相同的调用」拦掉，功能形同失效。

**测试**：`tests/smoke.py::case_http_diff`（6 组断言：schema 有 diff / 首次无基线 / 改 id 三行差异且不回全文 /
同 URL 再 diff 仍联网且无差异 / 结果进缓存 / 新 path 兜底 / 400 行全变截断）。

**坑**：`count` 时别把 `--- baseline`/`+++ current` 文件头算成删除/新增（`index < 2` 跳过）；
`_render_http` 签名**别动**（`case_limit_and_kwargs` 等按位置传 7 参）。

---

## 7. 之后的功能（按优先级）

1. method_cards 扩 23→50+：**需 Cybench 轨迹日志**（Google Drive，沙箱不可达；用户可本地下载后交给你）。同时统计「成功 web 题平均几步」校准 `web.max_steps`（现在 10 常出不来 report）。
2. 可选：http_request 并发/批量（省调用次数，风险是限流，谨慎）。
3. **B1 容器适配层（放最后，用户明确）**：`agent_main.py` 不存在、`Dockerfile` 的 `ENTRYPOINT` 还是注释。方案在 `docs/b1-adapter-plan.md`，可纯离线验收（fake MCP + task.json fixture）。**不做 = 交上去 0 分**。

---

## 8. 复用的小知识（坑）

- `--config`/`--run-id` 是 argparse **全局**参数，放子命令前。
- 预算**跨 run 累计**：重跑同一道题前要重置 state（`ch.tokens={}; ch.status='new'; ch.instance_id=''; ch.instance={}`）。更省事：直接换新 `--run-id`。
- `store.meta["competition_id"]` 只在 `cli.py challenges` 子命令写过；`crew` 路径靠 `_competition_id` 的 membership 兜底。
- web 专家每轮 delegate 是**全新 messages**（历史不跨轮），靠 `_delegate` 的 `_prior_digest`（账本摘要）+ http body 缓存 + `note` 共享维持记忆——机制已存在，别重复加。
- `http_request` 已有 grep/offset/limit/5000窗口/body缓存桩/Range拦截/redirect校验/`diff`——**别重做**。
- `diff` 的基线必须在 `_cache_put` **之前**取；`_ledger_gate` 对 `diff=true` 是放行的（ledger 不看 diff）。
- smoke 里 `case_crew` 等 mock 配置自带 `platform: ["read","instance","oracle"]`，**不要**改成白名单（会收窄覆盖）。

---

## 9. 一句话给用户的第一段话（见回复，直接复制粘贴）
