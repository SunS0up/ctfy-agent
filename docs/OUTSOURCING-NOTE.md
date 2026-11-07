# 交接说明：AI 安全外包（给同项目的另一个对话）

> 作者：本对话（只读审计 + 材料包制作）｜ 日期：2026-10-25
> 本对话**没有修改任何源代码**，只新增了 `dist/ai-kit/`、`dist/ai-kit.zip` 和本文件。

## 1. 结论：只外包 AI 安全，不外包 Crypto / Reverse / Misc

依据赛事手册第五节（`runs/handbook-zh-CN.md:46-60`）：

> 智能体赛道出题方向**仅限于在线交互类**。
> 离线附件类：Crypto / Reverse / Misc
> 在线交互类：Web 安全 / Pwn / **AI 安全**（AI 开源框架及智能体相关漏洞的挖掘与利用）

这条规则我们自己的文档早已记录过：
- `docs/why-authoring.md:15-18`：Crypto/Rev/Misc 明确不在智能体赛道出题范围内
- `docs/report-ctfy-rules.md:278`：智能体赛道仅限在线交互类
- `docs/plan-v1.md:31`：只投入 Web / Pwn / AI 三套工具链

所以：
- ❌ Crypto / Reverse **不外包**（规则外；且 rev 缺 `file`/`strings`/`objdump`，属工具链问题，不是提示词问题）
- ❌ Misc **不再追加投入**（已合并的保留，不删代码）
- ✅ **AI 安全外包**（规则内、我们零角色零提示词零夹具、纯 `http_request` 即可覆盖）
- ⚠️ Pwn：规则内但我们完全无能力（无 `nc`/`gdb`/`objdump`），需先确认题池占比再决定

## 2. 为什么 AI 安全可以"零代码合并"

`src/ctfy_agent/crew.py` 的三条事实：

1. `_coordinator_tools()`（约 269-278 行）注释写明「delegate 的候选角色**从 roles 派生，新角色无需改代码**」，enum 每轮从 `self.roles` 重建；
2. `Crew.__init__`（约 233-238 行）对 `config.extra["roles"]` 做深合并，未知角色直接新增；
3. `_role_specs()`（约 251-267 行）按名字白名单挂本地工具（`http_request`/`shell`/`decode_blob`/`unpack_archive`/`hexpeek`）。

结论：**新增 `ai` 角色只需改 `config/agent.json` 的 `roles`（配置，不是代码）**；`src/` 一行都不用动。

## 3. 本对话做了什么

```
dist/ai-kit/                     # 给外包方的材料包（17 个文件，63.6 KB zip）
├── README.md                    # 第一步读：交付清单 / 验收命令 / 禁令
├── AI-PROMPT.txt                # 发给外包方 AI 的第一段话（整段复制）
├── KIT-MANIFEST.json            # 文件清单 + 体积 + 哈希（已声明不含凭据）
├── docs/
│   ├── ai-module-brief.md       # 工单：交付物 / 契约 / 验收 / 禁令 / 合并步骤
│   └── rules-excerpt.md         # 赛事规则摘录 + "什么算 AI 安全题"判据
├── contracts/
│   ├── role-schema.md           # 角色键名、平台工具组、本地工具白名单
│   ├── prompt-contract.md       # 提示词必须覆盖的 8 类攻击与写法要求
│   ├── fixture-format.md        # 夹具四件套 + mock 服务 + 参考解契约
│   └── tool-surface.md          # 工具签名与边界（5000 窗口 / 无 JS / 120s）
├── examples/fixture-format/01-nested-b64/   # 已验收的真实夹具（范式）
└── reference/                   # 只读参考：tools_local.py / crew.py / solver.py / config.py / agent.json
dist/ai-kit.zip                  # 可直接发给外包方
```

包内**不含**：`.env`、`runs/`、`HANDOFF.md`、`PROGRESS.md`、`docs/mcp-tools.json`、任何 token 或实例地址。

## 4. 外包方交回来之后，合并步骤（需要改代码的只有第 3 步）

1. **角色配置**（配置，不是代码）：把 `roles/ai.json` 的 `ai` 键并入 `config/agent.json` 的 `roles`，
   `system` 填 `roles/ai.system.txt` 全文。`platform` 用组名 `["read","instance","oracle"]`，
   `local` 用 `["http_request","shell"]`，`allow_shell: true`。
2. **夹具落盘**：`tests/fixtures/ai/NN-*/` 三道，逐道 `cd` 进去 `python solve.py` 验证输出。
3. **加 smoke 用例**（代码，需在允许改代码的会话做）：`tests/smoke.py` 加 `case_ai_*` 并注册。
4. **回归**：`python -m compileall -q src tests`；`python tests/smoke.py`（当前基线 **44 passed / 0 failed**，只允许变多）。
5. **端到端**（可选）：若题池里出现 AI 安全题，跑一次 `crew <题目>`；否则先作为能力储备。

## 5. 未决事项（需要人确认，不要猜）

1. **正赛题池里 AI 安全占几道**——我们只见过练习池（50/50 全是 web），AI 安全题形态未知。
   夹具按最常见的三类（系统提示词泄露 / 工具调用滥用 / 越权泄露）设计，见到真题可让外包方补一道。
2. 若真题需要**本地跑模型**（GPU / transformers），我们做不到；这是结构性限制，提前接受。
3. **Pwn** 是否投入，取决于题池占比；投入则属于镜像 + 代码改动（`nc`、二进制工具、新角色），不是这次外包范围。

## 6. 与本项目现有计划的关系

- 不改变 A0/A1/A2 的验收：A0 已落地（44/44），A1 账本已落地，A2 证据契约已落地。
- 与 B1 交付线并行：B1 是镜像能否跑起来的前提，优先级高于本外包的合并。
- 本外包的合并冲突面极小（只动 `config/agent.json` 的 `roles` 与 `tests/smoke.py` 的用例注册）。
