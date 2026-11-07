# misc 模块交接单（给队员 + 给他的 AI 第一段话）

**用途**：把 misc 能力外包给队内某人时，照这份单子给东西、交代信息、发开场提示词。
**配套**：`docs/misc-module-brief.md`（工单 v2，技术契约全文）、`dist/misc-kit/`（现成材料包）。

---

## 1. 给哪些文件（就发这一个包，别自己挑）

```bash
python tools/make_misc_kit.py --verify --zip
# → dist/misc-kit/  和  dist/misc-kit.tar.gz（实测 127 KB，kit 内 48 passed）
```

包里有什么（实测 **29 个文件 / 127 KB**，全部白名单内，无 `__pycache__`）：

| 包内路径 | 为什么给 |
|---|---|
| `RUN-TESTS.md` | **他第一步该读的东西**：三条自测命令 + 交付物清单 + 禁令 |
| `docs/misc-module-brief.md` | 工单 v2 全文（§3.1 角色配置字段、§3.2 新增工具三处改动、§3.4 交付契约、§5 验收） |
| `docs/report-ctfy-rules.md` | 运行时约束的来源（无外网、实例时限等），写提示词时要照着来 |
| `src/ctfy_agent/**` | 只读参考：`tools_local.py`（工具契约范本）、`crew.py`（角色装配）、`solver.py`（步数/截断）、`config.py` |
| `src/ctfy_agent/testing/**` | `fake_llm` / `fake_mcp_server`：他写 `case_misc_*` 时要用的离线夹具 |
| `tests/smoke.py` | 基线自测；`case_role_extension` 是「新增角色」的标准范例；`case_misc_contract` 是交付后的验收器 |
| `config/agent.json` | roles 的真实样例 |
| `Dockerfile` | 镜像里有哪些命令（决定提示词能写什么命令） |
| `roles/README.md`、`tests/fixtures/misc/README.md` | 产物该放哪、夹具四个文件叫什么 |
| `KIT-MANIFEST.json` | 基线声明（48 PASS）与来源 |

## 2. 绝不能给（脚本已硬拦，但你手动发文件时要记得）

| 不给 | 原因 |
|---|---|
| `.env` | 含真实 `CTFY_TOKEN` / `DEEPSEEK_API_KEY` |
| `runs/` | 真实转录、state.json、可能含真 flag 与实例地址 |
| `HANDOFF.md` / `PROGRESS.md` | 内部判断、`competition_id`、历史结论 |
| `docs/mcp-tools.json` | 980 KB 平台工具全量 schema，非必要 |
| 平台 token / 队伍凭据 / 任何比赛实例地址 | 他不需要碰平台；无外网，给了也连不上 |

> v1 工单的手敲 `cp` 命令把 `HANDOFF.md` 也打了进去——这是 v2 改用脚本的直接原因。

## 3. 必须口头交代的 6 条信息（文件里没有或容易漏读）

1. **这是「能力」外包，不是「题」外包**：他交的是跑在我们镜像里的代码+提示词，不是一个能联网的 AI 服务。
2. **他现在有的 misc 题是 0 道**（题库 50/50 全是 web；赛道只收 Web/Pwn/AI）。所以夹具要他自己造 3 道，这是唯一验收方式。
3. **分工**：他能跑的是「离线单测 + 夹具参考解」；**Crew 端到端解题只有我们能跑**（需要模型网关和预算，他没有 key）。这条一定要先说，否则他会卡在「要求解出 2 道」上。
4. **只能改这四个地方**：新增 `roles/`、新增 `tests/fixtures/misc/`、在 `tests/smoke.py` 里加 case 并注册、在 `tools_local.py` 里加 SPEC/方法。其余文件（`crew.py`/`solver.py`/`ledger.py`/`platform.py`/`state.py`/`cli.py`）**禁改**——这些一天内被改过多次，非增量补丁必冲突。
5. **只收增量**：diff 或新增文件，不收整文件覆盖。
6. **截止时间 + 回传方式**（你填）：建议先只要方案确认，第一版只交 `roles/misc.system.txt`，工具和夹具等入围题池确认后再做。

## 4. 给他的 AI 的第一段话（复制粘贴，整段发）

```
你是一名 CTF 杂项（misc）方向的工程师，要为一个已经跑通的自动解题 agent 写一个「misc 能力模块」。
材料都在 misc-kit/ 里：先读 RUN-TESTS.md 和 docs/misc-module-brief.md（工单），再动手。

三条不可协商的约束：
1. 运行环境完全无外网（比赛容器 internet_policy=deny），任何依赖外部 API / 在线服务的方案都不可用。
2. 只能用 Python 3.12 标准库，或镜像里已有的 curl / grep / coreutils。
   没有 file、xxd、unzip、binwalk、zsteg、PIL、numpy ——用 stdlib 等价物代替。
3. 上下文窗口只有 16000 tokens，单次工具输出上限 6000 字符。
   不能把附件整个读进上下文，必须先落盘再切片。

你要交付（只交增量，不要整文件覆盖）：
1. roles/misc.json —— 可直接并入 config/agent.json 的 roles 片段（注意必须显式 allow_shell: true）。
2. roles/misc.system.txt —— misc 角色系统提示词全文，这是这个模块 90% 的价值。
   要教会模型「判别题型 → 选工具 → 逐层处理 → 卡住就收口」，每条命令必须是镜像里真能跑的。
   不要写 PLAN / report / evidence 的格式规则，框架会自动补；写了也不能与之冲突。
3. （可选）patches/tools_local.patch —— 新增 1~2 个本地工具。三处都要改：
   写 SPEC、在 LocalTools.specs() 里注册、写同名方法（漏了注册，call() 会直接报「未知本地工具」）。
4. tests/fixtures/misc/ 下 3 道自建夹具（多层编码 / 归档解包 / 简单隐写），每道含
   task.md、files/、expected.txt、solve.py（确定性参考解：不联网、不调 LLM、只用 stdlib，stdout 打印 flag）。
5. tests/smoke.py 里新增 case_misc_* 并注册到 cases 列表。
6. misc-CHANGELOG.md：改了什么、为什么、怎么回滚。

两条最容易踩的契约（详见工单 §3.4）：
- report 的 evidence 字段必填，且必须是某一次工具输出里的“逐字连续片段”，改写/概括/翻译都会被拒；
- 同一个工具用完全相同的参数再调一次会被账本拦下（返回 [ledger 拦截]），这是设计行为，不是你工具的 bug。

验收（你自己就能跑，全部离线，不需要 token / 联网 / Docker）：
    python -m compileall -q src tests
    python tests/smoke.py                               # 期望 48 passed, 0 failed；只允许变多
    cd tests/fixtures/misc/01-xxx && python solve.py    # 输出必须包含 expected.txt 里那一行 FLAG{...}

禁止：
- 不要索取 .env、token、比赛实例地址（你不需要，也没有）；
- 不要在 solve.py 里直接读 expected.txt 打印（等于没验证，会打回）；
- 不要把「让模型执行任意命令」包装成新工具（会绕过已有沙箱约束）。

第一步不要写代码：读完 RUN-TESTS.md、工单全文和 tests/smoke.py 里的 case_role_extension，
先给我一份方案确认——打算做什么、明确不做什么、为什么、预计改哪些文件。我确认后你再动手。
```

## 5. 只想发一句话时的版本

```
先读 misc-kit/RUN-TESTS.md 和 docs/misc-module-brief.md，按里面的契约做 misc 能力模块；
限定 Python 3.12 标准库、无外网、只交增量。读完先给我方案确认，不要急着写代码。
```

## 6. 他交回来之后，我方三步

```bash
# 1) 应用增量（先纯新增，零风险）
#    roles/ → tests/fixtures/misc/ → tools_local.py patch → tests/smoke.py case
python -m compileall -q src tests
python tests/smoke.py                      # 期望 ≥48 passed，0 failed

# 2) 契约验收（自动查 roles / 工具注册 / 夹具参考解 / 账本去重）
python tests/smoke.py 2>&1 | grep -i misc_contract

# 3) Crew 端到端（只有我们能跑，3 道夹具至少解出 2 道；等 B1 适配层完成后在容器里跑）
```

> 合并时若 `misc_contract` 报错，先跑 `python tools/misc_contract_selftest.py`：它 5/5 通过说明
> 「验收器本身没问题」，问题在他的交付物。
