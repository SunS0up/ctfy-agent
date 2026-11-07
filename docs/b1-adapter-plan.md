# B1 容器适配层 —— 完善方案 + 可行性审核

> 状态：**方案待批准，未改动任何代码**。所有平台侧结论均从 `.tools/ctfy-src`（ctfy 0.7.0 wheel）逐行读出，
> 本项目侧结论均标注当前行号。基线：`tests/smoke.py` 47/47、`compileall` 干净。
>
> 日期：2026-10-25

---

## 0. 结论摘要（先看这个）

| 判断 | 结论 |
|---|---|
| **契约层是否吃得准** | ✅ 吃得很准。task.json / result.json / 入口方式 / 网络 / 超时 / 计分口径全部有源码坐标，无猜测。 |
| **B1 能否纯离线做完并验收** | ✅ 能。fake MCP + 脚本化 LLM + task.json fixture 可覆盖 8 类分支，零 token。 |
| **B1 做完是否等于"镜像可用"** | ❌ 不等于。还差 B2（流式，健壮性）+ B3（有 Docker 才能 build）+ 一次真实 dispatch 验证。 |
| **最大阻塞项** | ⚠️ `LLM_FLAVOR` 必须是 `openai`。我们的客户端只说 OpenAI 协议（`llm.py`），若注册的是 `anthropic` 线路由，镜像直接不可用（补 Anthropic 客户端 +0.5 天）。 |
| **最容易被忽略的致命点** | ⚠️ 默认配置 `max_llm_calls=20`（`config.py:40`）会让 60 turn 的题在第 20 次调用就被预算刹车掐死；且 `solved` **不由我们上报决定**，必须真提交。这两条都在下面详述。 |
| **工作量** | 约 1 天（核心 0.5 + 用例 0.25 + Dockerfile/文档 0.25）。 |

---

## 1. 平台契约事实（逐条有坐标，别再去网上找文档）

### 1.1 启动方式：平台不传命令，entrypoint 必须由镜像自带

`docker create --network <net> -w /ctfy/work -e CTFY_WORK_DIR=/ctfy/work ... -- <image>`
（`providers/docker_provider/harness.py:975-1046`，镜像名是最后一个位置参数，**之后没有任何 command/args**）。

推论：
1. Dockerfile 必须有 `ENTRYPOINT`（或 CMD），否则容器起来就退出 → probe 报 `did_not_start`/`no_result_file`。
2. cwd 被强制成 `/ctfy/work`，**相对路径 entrypoint 会死**（`harness.py:208-216` 有实测记录：`ENTRYPOINT ["python","agent.py"]` 报 `can't open file`）。
3. 必须用绝对形式：`python -m ctfy_agent agent` + `ENV PYTHONPATH=/ctfy/src`（不依赖 cwd）。

### 1.2 结果文件：必须写文件，stdout 只在"整段是 JSON"时兜底

- 用户镜像 `requires_result_file=True`（`server/eval_runs.py:1187` 的 `requires_result_file=foreign`），
  路径固定 `$CTFY_WORK_DIR/result.json`（`eval_runs.py:1188`、`eval/harness.py:143-146`）。
- 文件读不出来时分五种（`harness.py:600-704`）：
  `absent / not_regular(符号链接，O_NOFOLLOW 拒读) / unreadable / oversized(>4MiB, harness.py:598) / invalid_json`。
  前四种会被平台渲染成不同的报错，但**都等于 0 分**。
- 没有文件时，平台才回退到 stdout，且是 `json.loads(整个 stdout)`（`eval_runs.py:1550`）——
  一行日志就毁掉。所以：**日志一律走 stderr，stdout 只输出一行最终 rollup JSON**。

### 1.3 rollup 字段与语义

必填/被消费的字段（`eval/harness.py:125-137` 是平台自己的写法，`eval_runs.py:2357-2470` 是读取点）：

```
challenge_id, instance_id, solved, fully_solved,
input_tokens, output_tokens, turns, tool_calls_made, stop_reason
+ 可选 writeup（会被 clip）、answers（会被平台代提交）
```

三条关键语义：
1. **`solved` 不由我们决定**。平台按 `submissions` 表在自己窗口内的记录重算（`eval_runs.py:2965-3012`）。
   → 光写 rollup 说自己解了，一分没有；**必须真提交**（`submit_answer` 或 `answers` 交接）。
2. **`stop_reason` 只认 `exit_<n>` 前缀判"非正常结束"**（`eval_runs.py:2824-2890`）。
   `exit_0` 不算；`final / max_turns / halted / cut_off / refused` 都按原样入库。
3. **`answers` 交接有前置条件**：`rollup["instance_id"]` 必须是本队实例，否则平台直接跳过
   （`eval_runs.py:2511-2530`）。所以 `instance_id` 必须写对。

### 1.4 环境变量：BYO 镜像拿到的比 HANDOFF §6 写的少

真实注入（`eval_runs.py:334-387` 的 `_harness_env`，加上 `with_sdk_native_names`）：

| 变量 | 是否注入 | 备注 |
|---|---|---|
| `LLM_FLAVOR` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` | ✅（MODEL/BASE 视模型行是否配置） | openai flavor 时 `LLM_BASE_URL` 形如 `<gateway>/v1`（`challenge/llm_stub.py:81-86`） |
| `LLM_CONTEXT_TOKENS` | 可选 | 模型行配了才有 |
| `CTFY_PLATFORM_URL` / `CTFY_TOKEN` | ✅ | |
| `CTFY_EVAL_CHALLENGE_ID` / `CTFY_EVAL_COMPETITION_ID`（可选）/ `CTFY_EVAL_MAX_TURNS`（可选） | ✅/可选 | competition_id **可能没有** |
| `CTFY_EVAL_KEEP_INSTANCE=1` | ✅ 恒有 | 见 §1.6 |
| `CTFY_WORK_DIR` | ✅（`-e` 由节点加，`harness.py:1010-1020`） | |
| **`CTFY_MCP_URL`** | ❌ **不注入** | 只在平台自有 harness 里出现（`eval/external.py:871`） |
| **`CTFY_TARGET_URL`** | ❌ **不注入** | 同上（`external.py:870`） |

→ **HANDOFF §6 的变量清单把"平台自有 harness"和"BYO 镜像"两条通道混在一起了**，需要修正。
→ MCP 端点只能两条路得到：读 `$CTFY_WORK_DIR/mcp.json`（平台写好的客户端配置，`harness.py:775-796`、
`core/agent_brief.py:550-582`），或 `CTFY_PLATFORM_URL + /api/v1/mcp/`（`agent_brief.py:509` 的 `MCP_PATH`，带尾斜杠）。

### 1.5 干跑（dry run）是注册门，且什么凭证都不给

- probe 的 brief：`run_mode=instance` + `dry_run=True` + `platform_url=""` + `requires_result_file=True`
  （`server/registry_conformance.py:252-285`）；容器环境 `env={}`（`:313`），**没有 token、没有 LLM key、没有 platform_url**。
- 预算 `DRY_RUN_BUDGET_S = 180`（`core/harness_conformance.py:277`）。
- 判定（`harness_conformance.py:280-345`）：`no_result_file` 是 **blocking**；`nonzero_exit`、`no_schema_version`、`dry_run_ignored` 只是 advisory。
- README 明确要求 dry run 写 `"stop_reason": "dry_run"` 并 `exit 0`（`core/agent_brief.py:585-605`）。

→ 适配层**第一个分支**必须是 dry_run：零网络、直接写 rollup、exit 0。

### 1.6 实例必须留着

`CTFY_EVAL_KEEP_INSTANCE=1`（`eval_runs.py:358`）——平台自己回收，且 checker 类题在容器退出后
才由题目自己的 judge 判分（`eval_runs.py:2575-2580` 注释）。
我们 `Crew.solve` 的 `finally` 默认会 `stop_instance`（`crew.py:534-546`）→ 容器里必须显式关掉。

### 1.7 超时后平台仍会读 result.json

超时分支 `_reap` 只 stop 不 rm，随后 `_copy_rollup_out` 仍去读文件（`harness.py:1938-1958`）。
→ **启动即写一版 provisional rollup**，被 kill 也有东西可读，避免"运行了 40 分钟却报没写文件"。

### 1.8 网络与隔离

- 用户镜像以 `UNTRUSTED` 跑：`--cap-drop ALL`、`no-new-privileges`、pids/memory/cpus 上限；
  实例类 dispatch 用 `own_network=True` 自建 bridge（`eval_runs.py:1401`）。
- 平台强制 `CTFY_PLATFORM_URL` 必须是"容器能拨到"的地址，否则直接拒跑（`eval_runs.py:1108-1120`）。
- 实例地址从 `start_instance` 的 `surface.services[].host` 来，实测是公网域名（`*.node4.ctfy.run:443`），
  bridge 出网可达。

### 1.9 模型与网关

- `LLM_BASE_URL`：openai flavor = `<gateway>/v1`，anthropic flavor = `<gateway>` 无后缀（`llm_stub.py:81-86`）。
- 网关会**用注册模型改写请求里的 model**（`llm_gateway/proxy.py:684-700` 注释），
  所以多角色多模型名毫无意义，容器里统一用 `LLM_MODEL`。
- 网关**非流式也支持**（`proxy.py:621` 读 `stream` 标志，`:1041-1059` 的 `_complete` 是非流式分支），
  所以 "必须流式" 不是硬约束（HANDOFF §7 这句要改成"建议流式"）：流式主要防长思考撞 120s 客户端超时与上游空闲超时。

---

## 2. 本项目现状差距（容器里会踩的 8 个坑）

| # | 坑 | 证据 | 后果 |
|---|---|---|---|
| G1 | 镜像不带 `config/agent.json`（`Dockerfile:23` 只 COPY src） | — | 角色 profile 全走 `DEFAULT_ROLES`（`crew.py:49-55`），web 角色 `allow_shell=False`，curl/grep 装了也用不上 |
| G2 | 无配置文件时 `DEFAULTS` 生效：`max_llm_calls=20`、`max_tokens_per_challenge=300000` | `config.py:39-40` | 60 turn 的题第 20 次调用即被掐断；白丢一半预算 |
| G3 | `max_tokens=4096` | `config.py:29` | 平台自己的注释写明 4096 会在 thinking 块里截断、模型吐不出 tool_call（`.tools/.../eval/llm.py:44-49`）→ 一步即"无动作结束" |
| G4 | `runs_dir="runs"` → `PROJECT_ROOT/runs` = `/ctfy/runs` | `config.py:16,46` | 转录/state 不进 `/ctfy/work`，artifact 拿不到，赛后无法验尸 |
| G5 | `Crew.solve` 退出时停实例 | `crew.py:534-546` | checker 题判分时实例已没了 |
| G6 | `_submit` 默认 `question_id="flag"` | `solver.py:423` | 题目 question id 若不是 `flag`，平台判 `unknown_question`（`solve_recording.py:566-575`） |
| G7 | `ensure_challenge_meta` / `cmd_*` 往 **stdout** 打日志（`cli.py:113`） | `cli.py:87-114` | stdout 被污染；文件写失败时平台兜底解析必炸 |
| G8 | MCP 地址只认 `CTFY_MCP_URL`/配置（`config.py:50`） | `config.py:49-52` | 容器里该变量不存在 → 连不上平台 |

---

## 3. 方案总览

### 3.1 新增/改动文件

| 文件 | 动作 | 内容 |
|---|---|---|
| `src/ctfy_agent/agent_main.py` | **新增**（约 380-450 行，纯 stdlib） | task.json 解析、env→Config、MCP 端点解析、挑战入库、dry_run/run_mode 分支、Crew 执行、rollup 组装与原子写、信号处理、顶层兜底 |
| `src/ctfy_agent/cli.py` | 改动（约 12 行） | 加 `agent` 子命令 + `--task` 参数（`build_parser` 内一处，`handlers` 一处） |
| `Dockerfile` | 改动（约 6 行） | `ENV PYTHONPATH=/ctfy/src`、`COPY config/`、`ENTRYPOINT ["python","-m","ctfy_agent","agent"]`、apt 加 `bash` |
| `tests/smoke.py` | 改动 | 新增 4 个用例（见 §6） |
| `tests/fixtures/*.json` | 新增 | 5 份 task.json fixture |
| `docs/b1-adapter-plan.md` | 本文件 | — |

### 3.2 启动时序（确定性，无隐藏分支）

```
main()
 ├─ 解析 work_dir = CTFY_WORK_DIR or /ctfy/work
 ├─ 读 task.json（宽容；坏了也继续，但 record 问题）
 ├─ result_path = task.result.path or work_dir/result.json
 ├─ 【写 provisional rollup】stop_reason="halted", note="provisional"   ← 保证任何时刻都有文件
 ├─ 注册 SIGTERM/SIGINT handler（handler 内直写 rollup 后 os._exit）
 ├─ if task.dry_run:  rollup(stop_reason="dry_run") → exit 0            ← 零网络，必须最先
 ├─ if run_mode != "instance": rollup(stop_reason="refused") → exit 0   ← 明确不支持，不烧钱
 ├─ 组 Config（env 映射，见 §4.1）
 ├─ 校验 token / LLM key / LLM_FLAVOR；缺 → rollup("refused") → exit 1
 ├─ MCP: 读 mcp.json → url；握手失败 → rollup("halted") → exit 1
 ├─ seed challenge（task.json 题面，不走 cli 的打印路径）
 ├─ 可选增强：get_challenge 补 tags/difficulty（失败只 warn）
 ├─ target_instance 非空 → get_instance 取 surface，禁 auto_start
 ├─ Crew.solve(challenge_id)  ← keep_instance=True
 ├─ 组装 rollup（§4.3）→ 原子写（tmp + os.replace）→ stdout 打一行 JSON
 └─ exit 0（正常收尾）；顶层异常 → rollup("exit_1") → exit 1
```

### 3.3 设计原则

1. **任何路径都要写 rollup**：宁可写一份"什么都没做"的，也不能"没文件"（后者是 blocking）。
2. **确定性优先**：不做重试猜测，不做自动降级到别的 run_mode。
3. **stdout 只留一行 JSON**，所有进度/告警走 stderr，同时镜像一份到 `$WORK/runs/<cid>/agent.log`。
4. **不引入第三方依赖**（比赛约束），MCP/LLM 全走现有 urllib 实现。

---

## 4. 详细设计

### 4.1 env / task.json → Config 映射

| 来源 | 目标 | 规则 |
|---|---|---|
| `task.challenge.{id,name,description}` | `Challenge(id,name,description)` | 直接 upsert；**不从 `list_challenges` 反查** |
| `task.questions[].{id,prompt}` | `Challenge.description` 追加块 / `extra["question_ids"]` | 单题时作为默认 `question_id`；多题时把 id 列表写进题面 |
| `task.competition_id`（优先）或 `CTFY_EVAL_COMPETITION_ID` | `extra["competition_id"]` | `start_instance` 与 `list_hints` 必填（`verification.md` §2） |
| `task.target_instance` | `extra["target_instance"]` | 非空 → 不 auto_start；`get_instance` 取 surface |
| `task.max_turns` | `max_llm_calls` | 1 turn ≈ 1 次模型调用（平台自有 harness 同轴）；未给则用 `AGENT_MAX_LLM_CALLS` 或 80 |
| `task.max_turns` | 协调轮数 / 专家步数 | `rounds=max(3, ceil(turns*0.35))`，专家 `max_steps=max(4, ceil(turns*0.3))`，且受总调用数硬约束 |
| `CTFY_TOKEN` | `config.token` | 缺失 → 非 dry_run 直接拒跑 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 同名 | 缺失 → 拒跑；`LLM_MODEL` 覆盖**所有角色的 model**（网关会改写，多模型只是误导） |
| `LLM_FLAVOR` | `extra["llm_flavor"]` | 不是 `openai` → 拒跑并写清原因（见 §7 风险 R1） |
| `LLM_CONTEXT_TOKENS` | `max_context_tokens` | 缺省 16000；给了就取 `min(值*0.4, 64000)`（保守，控 prompt 成本） |
| `CTFY_WORK_DIR` | `runs_dir = $WORK/runs` | 修 G4；`sandbox/` 也在里面，artifact 可取证 |
| `CTFY_EVAL_KEEP_INSTANCE` | `extra["keep_instance_on_success/fail"]=True` | 修 G5；平台自己回收 |
| `AGENT_*`（原有 18 个） | 原样保留 | 便于现场调参 |

**兜底默认值（agent_main 内显式写死，不吃 `DEFAULTS`）**：`max_tokens=8192`（修 G3）、
`max_tokens_per_challenge=800000`、`max_llm_calls=max_turns or 80`、`max_context_tokens=16000`、
`submit_limit=min(3, 平台 cap)`、`instance_ops_limit=5`、`oracle_limit=50`、`auto_start_instance=True`。

### 4.2 MCP 端点解析（修 G8）

```
1. 读 $WORK/mcp.json → mcpServers 第一项的 url；把 url/headers 里 ${VAR} 用 os.environ 展开（模板形式见 agent_brief.py:550-582）
2. 失败 → CTFY_PLATFORM_URL.rstrip('/') + '/api/v1/mcp/'
3. 都失败 → 报错并 rollup("halted")
```
只取 url；鉴权仍用 `CTFY_TOKEN` 走 Bearer（`mcp_client.py:123-133`），不解析 headers 里的其它字段（当前平台只有 Authorization）。

### 4.3 rollup 组装与 stop_reason 映射

```json
{
  "schema_version": 1,
  "challenge_id": "...", "instance_id": "...",
  "solved": false, "fully_solved": false,
  "input_tokens": 0, "output_tokens": 0,
  "turns": 0, "tool_calls_made": 0,
  "stop_reason": "final",
  "writeup": "…≤2000 字符…"
}
```

| 内部 reason | rollup stop_reason | 说明 |
|---|---|---|
| `flag_accepted` / `coordinator_done` / `no_more_actions` | `final` | 正常收尾（含 0 分） |
| `step_limit` | `max_turns` | 撞步数/轮数 |
| `budget_exhausted` | `cut_off` | 撞 token/调用/金额上限 |
| `llm_error` / MCP 中途断 | `halted` | 工具链故障 |
| 不支持的 run_mode / 缺凭证 / flavor 不符 | `refused` | 主动拒跑，语义最近 |
| dry run | `dry_run` | README 明确要求 |
| 未捕获异常 | `exit_1` + exit 1 | 平台按 `exit_` 前缀判 aborted |

- `turns = budget.snapshot()["calls"]`（跨角色共享：`crew.py:328-335` 建、`:368-371` 挂到协调者与专家的 LLM 上）。
- `tool_calls_made` = 转录里所有 `tool_calls` 数组长度之和（排除 `role=tool` 的结果记录，避免重复计数）。
- `solved = summary["submitted"]`（**仅真提交成功**；平台仍以自己的记录为准）。
- `answers` 兜底（可选，建议做）：当「oracle `verify_answer` 判 true」但「`submit_answer` 调用失败」时，
  把 `{question_id: answer}` + 正确 `instance_id` 写进 rollup，让平台在收尾时代提交
  （`eval_runs.py:2484-2540`）。**只交接 oracle 已判真的答案**，避免白烧平台的 attempt cap。
- `writeup`：`summary` 的 reason + 步数 + token + 账本统计 + 一句结论，clip 到 2000 字符（远小于 4MiB）。

### 4.4 写盘与幂等

- 原子写：`result.json.tmp.<pid>` → `os.replace` → 保证是普通文件（不会被 `O_NOFOLLOW` 拒读）。
- 启动即写 provisional；结束时覆盖。两次写都记一行 stderr 日志。
- 文件权限 `0o644`（平台以 root 读，无要求，但便于 artifact 里查看）。

### 4.5 观感与安全

- 题面 / questions prompt 一律走 `_untrusted()`（`crew.py:_brief` 已做，questions 拼进 description 时自动继承）。
- 不打印 token；mcp.json 里的 `${CTFY_TOKEN}` 只在内存展开。
- 不自动调 `view_hint`（扣费不可回滚，`platform.py:75-77` 注释）。
- `shell` 仍限制在 `$WORK/runs/<cid>/sandbox/`（`tools_local.py:552-553`），不污染工作目录根。

### 4.6 Dockerfile

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl grep coreutils ca-certificates bash && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PYTHONUTF8=1 PYTHONPATH=/ctfy/src
WORKDIR /ctfy
COPY src/ /ctfy/src/
COPY config/ /ctfy/config/          # 角色 profile；运行时被 env 覆盖
ENTRYPOINT ["python", "-m", "ctfy_agent", "agent"]   # 绝对模块路径，不依赖 cwd
```

---

## 5. 降级矩阵（每条都要有用例）

| 场景 | 行为 | stop_reason | exit | 是否阻断注册 |
|---|---|---|---|---|
| task.json 不存在 / JSON 坏 | 用 `$WORK` 兜底写 rollup + stderr 说明 | `halted` | 1 | 是（probe 也会没文件） |
| `dry_run=true` | 立即写 rollup | `dry_run` | 0 | 否 |
| run_mode ∈ {qa, defence, awd, engagement} | 不跑 | `refused` | 0 | 否 |
| 缺 `CTFY_TOKEN` / `LLM_API_KEY` | 不跑 | `refused` | 1 | 否（probe 无凭证也能过） |
| `LLM_FLAVOR` ≠ openai | 不跑，写清"本镜像只支持 openai 线路" | `refused` | 1 | 否 |
| MCP 握手失败 | 不跑（无法提交=必 0 分，跑也没意义） | `halted` | 1 | 否 |
| `get_challenge` 失败 | 用 task.json 题面继续 | — | — | — |
| 运行中 LLMError | Crew 内部 break，正常收尾 | `halted`/`final` | 0 | 否 |
| 顶层异常 | 写 rollup | `exit_1` | 1 | 否 |
| SIGTERM | handler 直写 rollup 后退出 | `halted` | 0 | 否 |

---

## 6. 验收指标与用例

| 编号 | 指标 | 方法 |
|---|---|---|
| **M-B1** | 离线端到端：fixture task.json + fake MCP + 脚本化 LLM → `result.json` 字段齐全、值正确、exit 0、stdout 仅一行 JSON | `case_agent_end_to_end` |
| **M-B2** | dry run 5s 内完成、零网络、`stop_reason=dry_run`、且**不需要 token/key** | `case_agent_dry_run`（不启 fake MCP，断言无连接） |
| **M-B3** | 降级矩阵 6 条各命中且不抛异常 | `case_agent_degraded`（参数化 6 个 fixture） |
| **M-B4** | 结果只来自文件：把 stdout 混入 200 行日志后仍能解析（证明不依赖 stdout） | `case_agent_result_contract` |
| **M-B5** | 原有 47 条 + 新增 4 条全绿、连跑两遍幂等、`compileall` 干净 | `python tests/smoke.py` |
| **M-B6**（真机，B3 后） | probe 通过（无 blocking finding）且一次真实 dispatch 产出可读 rollup | 平台 console |

fixture 清单：`task_instance.json`、`task_dry_run.json`、`task_target_instance.json`、
`task_multi_question.json`、`task_malformed.json`。

---

## 7. 可行性审核

### 7.1 判定为"可行且有源码依据"（B1 范围内）

1. task.json → Challenge/Config 映射：字段与语义已逐条核对（§1.4/§4.1）。
2. rollup 写入与字段：`eval/harness.py:125-137` + `eval_runs.py:2357-2470` 双向对齐。
3. entrypoint / cwd / COPY 布局：`harness.py:208-216, 975-1046`。
4. dry run 门：`registry_conformance.py:252-285`、`harness_conformance.py:277-345`。
5. keep instance：`eval_runs.py:358` + `crew.py:534-546`（已确认改配置即可，不用改代码）。
6. 超时兜底：`harness.py:1938-1958`（provisional rollup 有效）。
7. 两条提交通道：`submit_answer`（实测可用，`docs/verification.md`）与 `answers` 交接（`eval_runs.py:2484-2540`）。

### 7.2 判定为"本地无法验证，必须真机/外部确认"（黄）

| 项 | 为什么无法本地验证 | 影响 | 缓解 |
|---|---|---|---|
| `LLM_FLAVOR` 是 openai 还是 anthropic | 只有注册模型行才知道 | anthropic → 镜像完全不可用 | 启动即检测并拒跑；必要时补 Anthropic 客户端（+0.5 天） |
| LLM 网关 / 实例域名从 bridge 是否可达 | 需要真实节点 | 不可达 → 一次调用都发不出 | 启动做一次 `/models` 或 1 次 1-token 探活，失败立即 rollup 报清楚 |
| `eval_timeout_s` 实际值 | 部署侧配置（`eval_runs.py:417-442`） | 无法校准软时限 | 靠 provisional rollup + SIGTERM；不给也能跑 |
| 平台是否接受我们 `question_id` 取值 | 依赖题目 spec | 提交被判 `unknown_question` | 用 task.json 的 question id；无题号时保留 `flag` |
| pids/memory 上限对 Python+MCP 的影响 | 需要真实限额 | 目前看余量很大 | probe 会先跑一遍 |
| 镜像 build / 注册 / digest 审批 | 本机无 Docker、需组织审批 | 交付阻断 | B3/B4，需你提供环境与 key |

### 7.3 判定为"当前不可行/不做"（红）

| 项 | 结论 |
|---|---|
| qa / defence / awd 三种 run_mode | 不做，显式 `refused`。qa 需要 `submit_qa_answer` 且无实例；defence 要交 patch；awd 要长期驻留 + arena 地址。三条都不在 TPCTF 合规赛道（Web/Pwn）的主路径上。 |
| engagement（报告型） | 不做，显式 `refused`。交付物是 `PUT /report`，与现有"单 flag"模型正交。 |
| 无 Docker 时的镜像验证 | 不可行，必须等你提供 Docker 环境（B3）。 |

### 7.4 风险登记册

| 级别 | 风险 | 触发条件 | 处置 |
|---|---|---|---|
| 致命 | R1 `LLM_FLAVOR=anthropic` | 注册模型行是 anthropic | 需你确认；否则补 Anthropic 客户端 |
| 高 | R2 默认 `max_llm_calls=20` | 未显式覆盖（G2） | agent_main 显式设置为 `max_turns` |
| 高 | R3 `max_tokens=4096` 截断 thinking | 未覆盖（G3） | 容器内默认 8192，可用 env 调 |
| 高 | R4 `CTFY_MCP_URL` 不存在 | 依赖 HANDOFF §6 的错误清单 | 走 mcp.json / platform_url 推导 |
| 中 | R5 非流式撞 120s 超时 | 长思考 / 大输出 | B2 提前做（0.5 天） |
| 中 | R6 `question_id` 不匹配 | 题号不是 `flag`（G6） | 用 task.json 题号 |
| 中 | R7 转录不进 artifact | `runs_dir` 默认（G4） | 显式 `$WORK/runs` |
| 低 | R8 stdout 被日志污染 | 走 cli 的 print 路径（G7） | agent_main 不走 cli，日志全 stderr |

### 7.5 需要修正的已有文档

1. `HANDOFF.md §6` 环境变量清单：`CTFY_MCP_URL` / `CTFY_TARGET_URL` **不属于** BYO 镜像（只在 `eval/external.py`）。
2. `HANDOFF.md §7` B2 描述"容器里必须改成流式"：网关非流式也支持，应改为"建议流式（防长思考超时）"。
3. `HANDOFF.md §7` B1 描述：补上 entrypoint 必须自带、cwd=/ctfy/work、result.json 必须含 `schema_version`、keep_instance、provisional rollup 五条。

---

## 8. 工作拆分（6 步，每步可独立验收）

| 步 | 内容 | 验收 | 预估 |
|---|---|---|---|
| 1 | `agent_main.py` 骨架：task.json 解析 + Config 映射 + MCP 端点解析 + provisional/最终 rollup 写入 | 单测：解析 5 份 fixture，字段映射逐项断言 | 2h |
| 2 | dry_run / run_mode / 缺凭证 / flavor 四个前置分支 | `case_agent_dry_run` + `case_agent_degraded` 前半 | 1h |
| 3 | challenge 入库 + target_instance + Crew 接线（keep_instance） | `case_agent_end_to_end`（fake MCP + 脚本 LLM） | 2h |
| 4 | rollup 组装（turns/tool_calls/answers 兜底）+ 原子写 + 信号处理 | `case_agent_result_contract` | 1.5h |
| 5 | CLI `agent` 子命令 + Dockerfile | 本地 `python -m ctfy_agent agent --task fixture` 跑通 | 1h |
| 6 | 全量回归 + 文档（HANDOFF/PROGRESS/memory） | 51/51 绿、compileall 干净 | 0.5h |

依赖：步 1→2→3→4 串行；步 5 可与 4 并行。

---

## 9. 需要你确认的三件事（决定要不要扩范围）

1. **注册的模型 flavor 是 `openai` 还是 `anthropic`？**
   openai → 现方案直接可用；anthropic → 要么补一个 Anthropic 客户端（+0.5 天），要么确认赛道模型可选 openai。
2. **正赛题目是否都是 `run_mode=instance` + 单 flag（单 question）？**
   是 → 按本方案；有 qa/多题 → 要么加分支，要么接受"非 instance 一律 refused"。
3. **`eval_timeout_s` 大概是多少（如果知道）？**
   知道 → 加软时限（到点前 2 分钟主动收尾并写 rollup）；不知道 → 只靠 provisional + SIGTERM，也能跑。

---

## 10. 明确不做（避免范围蔓延）

- 不做 qa / defence / awd / engagement 四种 run_mode 的执行（`refused` + 说明）。
- 不解析 `README.md`（平台渲染件），只读 `task.json` + `mcp.json`。
- 不把 `config/agent.json` 的 mcp/llm/预算原样带进容器（一律 env 覆盖；只保留角色 profile）。
- 不引入任何第三方依赖，不改 MCP/LLM 传输实现（B2 另做）。
- 不在代码里自动调用 `view_hint`（扣费不可回滚）。

---

## 11. 审计勘误（2026-10-26，对照 CTFY-0001 v2 原文 + .tools/ctfy-src 源码）

本节是 §2–§5 的**最终权威版**，已落地到 `src/ctfy_agent/agent_main.py`；与上文正文冲突处以本节为准。

| # | 原方案错误 | 修正（代码已实现） |
|---|---|---|
| C1 | `solved` 写成布尔 `false` | `solved` 是**题目 id 列表**（§3 示例 `"solved": ["flag"]`）；`fully_solved` 才是布尔 |
| C2 | `stop_reason` 用 `"final"` / `exit_1` | 正常结束 = `"done"`（§3 示例）；**删除 exit_1**，任何路径都 `exit 0` |
| C3 | dry run 只判 `task.dry_run` | 双信号：env `CTFY_EVAL_DRY_RUN=1` **和** `task.dry_run:true`；dry run 无 mcp.json、platform_url 空、无凭证 |
| C4 | 有 `target_instance` 分支 | `instance` 模式示例**无** `target_instance`（那是 engagement 专用，已拒跑）→ 删分支 |
| C5 | `solved` 语义来自「summary.submitted」 | `answers` 才是平台代交通道（只交接 `flag_value`，即平台校验通过的答案）；`solved` 由平台按自己记录重算 |

**已确认无误的关键点**（实现时保留）：`schema_version=2`、必须 `exit 0`、绝对路径 entrypoint + `PYTHONPATH=/ctfy/src`、stdout 不被读、`answers` 平台代交、`result.path` 原子写普通文件 ≤4MiB。

**源码级确认**：`CTFY_EVAL_KEEP_INSTANCE=1` 确实注入（`.tools/ctfy-src/ctfy/server/eval_runs.py:358`、`eval/harness.py:93`、`eval/runner.py:623` "leave it for the dispatcher"）→ 容器内**永不自己 stop_instance**（`keep_instance_on_success/fail=True`）。

**三个未决问题（主办方未答复）的鲁棒处理**：

1. **flavor 未知**：启动读 `LLM_FLAVOR`；`openai` 走现有客户端，`anthropic` → fail loudly `refused`（唯一剩余风险，回话后再补客户端 +0.5 天）。
2. **run_mode/单多题未知**：按 `instance` 主路径；多题容忍（全部 question 拼题面 + `default_question_id` 交给 `_submit`）；`qa/defence/awd/engagement` 一律 `refused`；`gated_questions` 拼进题面提示「答对后重新 get_challenge」。
3. **timeout 未知**：启动即写 provisional rollup + SIGTERM/SIGINT 处理器直写后 `exit 0`，不做软时限。

**另发现并修复的 2 个实现级缺口**：

- G6：协调者 `submit` 工具规范只暴露 `answer`（无 `question_id`），`_submit` 硬编码默认 `"flag"` → 改为读 `config.extra["default_question_id"]`（`solver.py:471` 一行）。
- 角色 `model`（Kimi-K2.7 等）与 web 的 `max_tokens=4096` 会穿透到容器 → 启动时归一化：剥 `model`、统一 `max_tokens=8192`（否则 4096 截断 thinking 块）。

---

## 12. 主办方回复与交付约束（2026-10-26）

主办方（群内 `return 0;`）三条口径，改变了交付路径：

1. **「你要做测试的话，先用 mcp 接入做测试」** —— 测试走 MCP 直连，不再等镜像审核/打包上传。
2. **「后面开发会取消 agent 审核环节」** —— 人工审核镜像这一步会取消。
3. **「镜像不要太大，太大了传上去无法测试」** —— 体积是**硬约束**（无具体数值）；平台页有「连接 AI 智能体」按钮作为 MCP 接入入口。

### 对应措施

- **测试路径**：`doctor`（令牌 + MCP 握手 + 模型配置）→ `whoami` → `challenges` → `crew --challenge <id>`，全部用 `.env` 里的真实令牌/模型。
  注意：容器路径只写 `result.json`；MCP 直连测试是「本机 agent 直接对平台解题」，共用 Crew，但**不经过** `/ctfy/work`。
- **体积**：`.dockerignore` 排除 `tests/ docs/ runs/ __pycache__/ *.pyc`；`Dockerfile` 剥 `pip/setuptools/wheel/ensurepip/test/idlelib/lib2to3/pydoc_data/turtledemo/tkinter`（约 20MB）；`PYTHONDONTWRITEBYTECODE=1`。
  体积主体是基础镜像 `python:3.12-slim`（≈125MB 未压缩 / ≈45MB 压缩层），本机无 docker 无法实测。
- **当前唯一交付阻断**：本机无 `docker`/`podman`/`nerdctl`，无 WSL，无 Docker Desktop → **镜像无法在本机构建**，需另找构建环境（另一台机器 / 安装 Docker / CI）。

### 尚未落实的合规缺口（§11 之外的审计发现）

- `run_mode` 仍只认 `instance`，`qa/defence/awd/engagement` 一律 `refused`（若正赛出这些模式即 0 分）。
- 不读平台渲染的 `/ctfy/work/README.md`（规范 §1.2 推荐「读 README.md 照做」即可合规）。
- 角色只有 `web/rev/crypto/misc`，无 `pwn`（且 `misc` 无 shell）。
- 超时/信号路径写空 `answers`（不抢救 state 里已校验的 flag）。
