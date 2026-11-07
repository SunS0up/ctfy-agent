# 可借鉴数据整理（borrowed techniques）

> 结论先行：开源 benchmark 里能「直接抄」的是**工具设计与打分口径**，能「抄思路」的是
> **subtask guidance（= 我们的 method_cards）**；**答案 / 榜单分数 / 整段 prompt 不可抄**。

## 一、已核实的来源与口径

- **Cybench**（[arXiv:2408.08926](https://arxiv.org/abs/2408.08926)，ICLR 2025 Oral）：
  - 40 道专业级 CTF，来自 4 场比赛；每题有 description + **starter files** + evaluator。
  - 8 个模型 × **4 种 agent scaffold**：structured bash / action-only / pseudoterminal / web search。
  - 榜单分 **Unguided % Solved** 与 **Subtask-Guided % Solved**，且标注 **pass@1**。
- **关键校准事实（摘自论文摘要）**：无 subtask 提示下，最好 agent 解出的题，人类最快 **11 分钟**就解了；
  人类要 **24h54m** 的最难题，**无任何 agent 解出**。
  → 无提示 agent 的公开能力上限 ≈「人类 11 分钟难度」。网上「近乎 100%」基本都落在
  guided / pass@k / 小子集 三者之一。

## 二、口径对照：为什么他们看着比我们高

| 维度 | Cybench 等 benchmark | 我们（TPCTF 容器） |
|---|---|---|
| 源码 | 给 starter files（白盒，漏洞常写在代码里） | 黑盒活实例，源码要打穿 SSRF 才读得到 |
| 预算 | 基本无 token 硬顶，跑到解出为止 | 自设 500k（本轮已上调 800k） |
| 上下文 | 128k–200k | 自设 16k（本轮已上调 32k） |
| 重试 | 常跑 pass@k 取最优 | 一题一次、一次机会 |
| 计分 | 发现漏洞即可得子分（subtask） | 只认 flag |

## 三、能直接抄的（已落地 / 本轮新增）

1. **subtask guidance = method_cards**：Cybench 明确「guided」显著提分——我们的
   `method_cards.py` 本质就是按题型 tag 注入的廉价 subtask guidance，方向有实证背书。
2. **工具结果截断**：structured-bash scaffold 的精髓是「大输出不整段进上下文」——
   我们的 `http_request` 早已有 grep/offset/limit + 5000 字符窗口 + body 缓存桩。
3. **会话 cookie jar（本轮新增）**：登录 Set-Cookie 自动回传，a07 认证题 / a01 需登录触达
   IDOR 的高频路径不再靠模型手动抠 Cookie 头。缓存 key 同步加进 cookie，登录前后不串会话。

## 四、不能抄 / 要留神

- **答案与 flag**：题目完全不同，零迁移，且等于过拟合/作弊。
- **榜单分数当目标**：口径不同（见上表），没有可比性。
- **整段 prompt**：绑定对方的工具名 / 环境假设 / 模型特性，抄了会和 A0–A3 打架。

## 五、本轮落地清单

- `tools_local.py`：`http_request` 新增会话 cookie jar（`_capture_cookies` / `_cookie_header`），
  缓存 key 从 `(netloc, path)` 扩为 `(netloc, path, cookie)`。
- `config/agent.json`：`max_tokens_per_challenge` 500000→**800000**（对齐 B1 方案的容器默认），
  新增 `max_context_tokens` **32000**（原 DEFAULTS 16000）。
- `tests/smoke.py`：新增 `case_http_cookie_jar`；smoke **54 passed / 0 failed**。

## 六、待办（需要外部数据时）

- Cybench 的完整轨迹日志在 Google Drive（本沙箱不可达）。**若你能在本地下载**，我可以：
  1. 抽取 web 类成功轨迹 → 扩 `method_cards.py`（23 → 50+，每条有实证）；
  2. 统计「成功 web 题平均几步、结论第几步产出」→ 校准 `web.max_steps`（现在 10 常出不来 report）。
- `http_request` 的 `diff` 参数（两次响应差异）仍在 defer，cookie jar 优先级更高。
