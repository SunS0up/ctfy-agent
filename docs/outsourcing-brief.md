# 外包需求书：CTFy 平台 Web 挑战题（challenge package）

## 0. 一句话

按 CTFy 平台的出题标准（CTFY-0002），交付 **1–2 道可直接过审上线的 web 挑战题**（完整 package 目录 + 参考解法），本地 `ctfy authoring check` 必须 0 error。**不需要接触比赛平台账号，不需要提交 flag。**

## 1. 背景

CTFy 是清华大学 TPCTF 2026 智能体赛道使用的比赛平台。比赛要求参赛队在规定窗口内**自己出题**，题目经平台审计与人工审核后上线，供其他队伍（以及自动解题 agent）攻破。我们要外包的是"出题"这部分，不是解题。

## 2. 权威材料（照着做，不要凭经验猜）

| 材料 | 说明 |
|---|---|
| `CTFY-0002.md` | **出题包标准，唯一权威**。目录结构、`metadata.yaml`、compose、答案交付、判分、参考解、体积限制全在里面 |
| `CTFY-0001.md` | 平台智能体协议（本次出题不需要，仅供理解运行环境） |
| `handbook_zh.md` | 赛事手册（出题方向限定"在线交互类"） |
| `mcp-tools.json` | 平台 97 个工具的 schema，其中 23 个出题相关 |
| `ctfy` CLI | `ctfy authoring init` / `ctfy authoring check`，随平台 0.7.0 发布；源码见 `github.com/wangyihang/ctfy`（`ctfy/cli/authoring.py`、`ctfy/challenge/audit.py`） |
| `github.com/CTF-Arena/challenges` | 平台官方题库，**2848 道现成题 / 20 个 suite**，结构可直接参考 |

> 官方脚手架（强烈建议从这里起步，不要手写）：
> ```bash
> ctfy authoring init ./my-challenge --stack python-flask --difficulty medium
> ctfy authoring check ./my-challenge     # 本地审计，无需 token、无需联网
> ```

## 3. 交付物

```
my-challenge/
├── metadata.yaml              # 必填：name / description / difficulty / tags / questions
├── docker-compose.yml         # web 必需
├── services/<name>/Dockerfile # 每个 build 服务必需
├── solution/exploit.py        # 参考解，按 §6.2 契约
└── （可选）attachments/
```
外加：
1. 打包好的 `<slug>.tar.gz`（≤64 MiB，解包 ≤128 MiB，单文件 ≤64 MiB，≤2000 文件，**不得含符号链接**）。
2. `ctfy authoring check` 的完整输出原文（必须 0 error）。
3. `docker compose up` 的启动说明 + 预期解法说明 + 难度自评（easy/medium/hard 的依据）。
4. 一份"这题考什么"的 200 字说明（给审核人看的 `note` 素材）。

## 4. 硬性技术约束

- **平台：** `competition_id = c0d8b4e3dd3b42c095d63d7dfa66431f`，平台版本 0.7.0。
- **类别：** `category: web`。技术栈只能选：`go`、`python-flask`、`node-express`、`ruby-sinatra`、`php-apache`。
- **无外网：** 比赛期容器 `internet_policy=deny`。题面服务**不得依赖任何公网资源**（CDN、外部 API、在线字体都不行），依赖全部打进镜像。
- **实例时限：** 3 小时。题必须在这之内可被有经验的选手解出。
- **答案纪律（最重要）：**
  - `services/` 源码中**不得出现字面量 `FLAG{…}`**；
  - Dockerfile 不得 bake 答案；
  - build arg 不得命名为 `ANSWER_*`；
  - 答案只能由平台的 `flag-rotator` / `seeder` 在运行时注入；
  - `attachments/` 与题面 prompt 都不得泄露答案。
- **参考解契约：** 入参 `<host> <port>` 或环境变量 `CTFY_TARGET_HOST` / `CTFY_TARGET_PORT`；stdout 每行输出 `Q[<question_id>]: <value>`；退出码 0=成功、1=跑通但失败、2=崩溃、3=无输出、4=不可达。
- **元数据：** 题目 id 匹配 `^[a-z][a-z0-9_]*$`；`difficulty` ∈ easy|medium|hard；`tags` 非空。

## 5. 验收（逐条可执行，全部满足才付款）

1. `ctfy authoring check ./my-challenge` → **0 error**（warning 需逐条解释）。
2. 本机 `docker compose up` 起得来，入口 URL 可访问，健康检查通过。
3. `solution/exploit.py` 在本地跑通，能拿到答案。
4. 我方用平台做 dry run（`check_authored_challenge`）→ 无 refusal 码；上传后平台 smoke 通过。
5. 全包搜索 `FLAG{`、`ANSWER_` 无命中（排除 rotator/seeder 的合法引用）。
6. 交付物自包含：不引用私有仓库、私有镜像、需联网拉取的资源。

## 6. 禁止事项

- **不要**索取或使用比赛平台账号、token、队内凭据。上传与 smoke 由我方执行，平台返回的 JSON 会原样转给你迭代。
- **不要**提交任何 flag 或尝试访问比赛实例。
- **不要**从官方题库整题搬运（可以参考结构，`provenance` 必须如实写明改编来源）。
- **不要**引入需要联网构建的依赖。

## 7. 时间

- **交付截止：11 月 10 日**（比赛上传窗口 11/21 才开，人工审核有延迟，被拒还可能触发上传暂停，必须留出两轮修订余量）。
- 中间节点：第 2 天交第一版目录骨架 + check 输出；第 4 天交可启动的服务；之后按我方回传的平台审计意见修订。

## 8. 交付方式

打包发 `my-challenge.tar.gz` + check 输出 + 说明文档；代码与题目版权归我方，需签 NDA，不得复用或公开。
