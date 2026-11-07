# 为什么智能体赛道要出题？——官方要求与计分真相（2026-10-25 核实）

**结论先说**：出题是**手册写死的任务范围**，但在**当前计分配置下不产生任何榜单分数**。
它属于「合规 + 能力展示 + 单项奖」，**不是刷分手段**；而且时间窗口极窄（11/21 三个 2 小时），
成本远高于收益。**核心得分仍然只能是解 flag。**

---

## 一、官方原文：出题被写进任务定义（不是可选项）

`runs/site/handbook_zh.md`（官网 `GET /api/v1/competitions/{id}` 的 `handbook.zh-CN`）

> **L32**：智能体程序连接赛事环境，按照预设规则**独立完成**网络安全挑战的**出题与解题**等全部攻防任务，**运行期间无人工介入**。

> **L48**：赛事命题延续 CTF"硬核实战"的核心基因……**智能体赛道出题方向仅限于在线交互类**。

对照 L50–60，在线交互类只有三类：**Web 安全、Pwn、AI 安全**。
**Crypto / Reverse / Misc 属离线附件类，明确不在智能体赛道出题范围内。**

> **L93**：赛事规则见智能体赛道**附件**中的赛事规则文档。

实测 `attachments: []`（空数组），`GET .../attachments` 也返回 `{"attachments":[]}` ——
**判罚细则与出题加分细则至今未公开**，这是当前最大的信息缺口。

---

## 二、平台侧的真实参数（API 下发，比手册硬）

| 字段 | 值 | 含义 |
|---|---|---|
| `authoring_enabled` | `true` | 出题功能对本赛事开启 |
| `authoring_windows` | 3 个窗口，均为 11-21 | **10:00–12:00 / 14:00–16:00 / 18:00–20:00（CST）** |
| `authoring_rate_limit_count` | `5` / `7200s` | 2 小时内最多 5 次出题相关操作 |
| `authoring_uploads_per_window` | `1` | **每个窗口只能上传 1 次** |
| `authoring_smoke_quota` | `10` | smoke 试运行配额 10 次 |
| `authoring_review` | `human` | **人工审核**，不是自动过 |
| `authoring_cooldown_threshold` | 3 | 7 天内被拒 3 次 → **暂停 7 天**（`authoring_cooldown_pause_s=604800`） |
| `authoring_public_on_publish` | `false` | 发布后不自动公开 |
| `max_instances_per_team_effective` | `1` | 出题、解题共用这 1 个实例配额 |

**读法**：三个窗口共 6 小时、总共最多 3 次上传机会，且要过人工审核。
这不是"边解题边顺手出题"的节奏，必须**提前离线准备好包**，窗口内只做上传和修订。

---

## 三、计分真相：出题目前 **0 分**（关键）

这是本次核查最重要的一条，且有源码级证据：

1. **本赛事计分规则为空** → 走平台默认 `flag_count`
   - `GET /api/v1/competitions/{id}` 返回 `scoring_rule: ''`
   - 该字段在公开投影里是**原样输出**（`server/routes/competitions.py:393`），不存在脱敏
   - 空值 → `DEFAULT_RULE_NAME = "flag_count"`（`server/scoring/registry.py:53`）

2. **榜单排序按解出旗数**
   ```json
   ordering: [{"field":"flags_solved","descending":true},
              {"field":"solved","descending":true},
              {"field":"last_solve_at","descending":false}]
   ```
   没有任何出题项。

3. **只有 `peer_authoring` 规则才读取出题数据**
   - `server/routes/scoreboard.py:234-239`：`authored` 只在 `rule.name == "peer_authoring"` 时加载
   - `server/authoring_reception.py:63-72`：非 `PeerAuthoringRule` 一律返回 `scored=False`

4. **解自己出的题也不记分**
   `server/authored_scoring.py`：判定照常返回（让你能验证题目可解），但
   `record_correct_answer` 跳过 solve 行，成就与计数通道一并跳过 —— 即 **unranked**。

**推论**：在当前配置下，出题既不给你加分，也不能靠解自己出的题补分。
如果审核通过的题进入赛场供其他队解（`server/background.py:2715`「审核过的就是选手能打的」），
**别人解题会涨他们的 `flags_solved`，而你涨不了** —— 严格说是净负。

> 补充：若主办方在开赛前把规则改成 `peer_authoring` 并设 `authoring_weight > 0`，
> 出题才会计分。曲线是 `PeakedCurve`：**解出率 5%–25% 得满分**（`core/authoring_score.py`
> 的 `DEFAULT_LO=0.05`、`DEFAULT_HI=0.25`、`DEFAULT_FLOOR=0.20`），
> 太难（没人解出）和太易（人人都解）都拿低分。**目前 `scoring_rule` 为空，等于权重 0。**

---

## 三点五、官方 FAQ 佐证（2026-10-25 补，用户提供的官方 FAQ 表截图）

官方 FAQ 表（社群共享，截至 10.24）两条直接相关：

> **#22（10.24 已答）**：赛题期间，智能体将在**我们所有控制的网络环境中**进行答题，
> 只允许访问**必要的站点**（如大模型服务提供商、赛事平台等站点）。
> ——印证 `internet_policy=deny`，且确认"释放题目时智能体已提交并停止交互"。

> **#23（10.24 未答，回答空白）**：「**赛事说明中明确说需要AI出题**，
> AI出题和解题的环境是分开的吗？**出题的评分又是怎么排的呢？**」
> ——① 提问者对赛事说明的解读与本文 §一 一致：**出题确实是明文要求**，官方未纠正；
> ② **出题评分官方至今未公布**（回答空白），与本文 §三 看到的
> `scoring_rule=''`、`attachments=[]` 完全互相印证——不是我们查漏了，是规则还没发。

**含义**：出题评分细则处于"官方待发布"状态。若最终采用 `peer_authoring` 计分，
§三 的"0 分"结论会反转成"按解出率计分"；**在细则发布前，出题只能按成本项对待，
且镜像里应预置好题目包，做到细则一到、窗口一开即可上传。**

---

## 四、那"为什么还需要出题"？四条实际理由

1. **手册把它写成任务范围**（L32）。是否"必须"、不做出题是否判罚，附件未公开 —— **未知**。
2. **评测流程并不会强制它**：`server/eval_runs.py` 里 `authoring` 出现 **0 次**，
   评测只派发解题任务，没有 `CTFY_AUTHORING_*` 环境变量。
   所以出题只能由 **agent 自己在窗口内主动调用**那 23 个出题 MCP 工具（97 个工具中）。
3. **单项奖**：手册 L80「智能体团体赛道**创新挑战单项奖** 3000 元」。
   原文未点名"出题"，但创新性评审最可能看的就是自出题 —— 属**高可能、未确证**。
4. **能力展示 / 完赛度**：作为"独立完成出题与解题"的赛道，出题是唯一能证明
   "不只会在别人搭好的环境里找 flag"的证据。

---

## 五、对策略的直接影响

- **不要**把解题预算挪去出题。榜单只有 flag，出题 0 分，且窗口只有 6 小时。
- 出题应当**离线预置**：镜像里带好 1 个模板化的在线交互类题目包（Web 优先，
  因为栈最成熟、`ctfy authoring check` 可离线验证），窗口内只做
  `check → upload → 读 smoke → 按拒稿修订`。
- **上限按 1 道题设计**，不要按 3 道。每窗口 1 次上传、人工审核、
  拒 3 次停 7 天 —— 三次机会全押在不确定性上是亏的。
- 若最终确认 `scoring_rule` 会被改成 `peer_authoring`，再提高出题优先级；
  **在那之前，出题是成本项不是收益项。**

---

## 六、待确认（只能问主办方或等附件）

1. 出题是否**强制**？不做出题是否影响完赛资格或评分？
2. `scoring_rule` 会不会在 11-15 前改为 `peer_authoring`？`authoring_weight` 取多少？
3. 审核通过的题是否进入**本赛事**的可解池（影响他人得分）？
4. 出题窗口内 agent 如何被唤起（评测只派解题任务，无 `CTFY_AUTHORING_*`）？
5. 「创新挑战单项奖」的评审口径是否包含出题质量？

---

**证据文件**
- `runs/site/handbook_zh.md`（赛事手册全文）
- `runs/site/agent_track.json`（比赛全部字段，含窗口/配额/排序）
- `runs/site/standards/CTFY-0002.md`（出题包标准 v1，1285 行）
- `.tools/ctfy-src/`（平台源码 v0.7.0，本报告所有源码坐标出处）
