#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engagement run_mode：GAUNTLET 作业市场的交付形态是「一份报告」。

和 instance 模式的差别（见 docs/report-ctfy-rules.md §2.4）：

  - 没有逐题 flag，得分面只有一份 Markdown 报告；
  - 靶场由平台开好（task.json 的 target_instance / target_endpoints），
    自己不许再 POST /instances；
  - 报告可以反复改，交卷是 finalize_report，评分按条目（accurate/inaccurate/not_mentioned）。

所以这里的循环是「侦察 → 记录证据 → 写报告 → save_report → finalize_report」，
不复用 Crew 的 flag 流程：flag 流程的终点是 submit_answer，在这里会被平台直接拒。
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable

from .llm import LLMError
from .platform import CtfyPlatform
from .tools_local import LocalTools, ToolError

#: 报告工具：本地合成，不在 MCP 工具表里（模型看到的是这个壳，落盘走 save_report）
REPORT_SPEC = {
    "type": "function",
    "function": {
        "name": "submit_report",
        "description": (
            "提交最终报告。只有当你已经把范围内能查的都查完、并把每条发现的证据写进 Markdown 时才调用。"
            "调用后本次任务即结束，平台会用它评分。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "report": {
                    "type": "string",
                    "description": (
                        "完整 Markdown 报告。必须包含：概述、范围与方法、发现清单（每条含证据、"
                        "复现步骤、影响、修复建议）。恢复到的哨兵（FLAG{...}）必须逐字引用。"
                    ),
                }
            },
            "required": ["report"],
        },
    },
}


SYSTEM_PROMPT = """你是一名受客户委托做安全评估的渗透测试顾问。客户给了你一个（或少数几个）在范围内的目标，
以及一份参与规则。你的交付物是一份**报告**，不是一堆零散答案——客户会照着这份报告施工修复，
所以「发现了什么」和「怎么复现」必须能对上。

工作方式：

1. **先摸清公开面**。根页面、robots.txt、sitemap、静态资源、JS 里硬编码的路径与密钥、
   HTML 注释、错误页、响应头（Server / X-Powered-By / 框架指纹）都属于「app 公开发布的东西」，
   在范围内。把每个请求记下来（方法 + 路径 + 状态码），这些就是复现证据。
2. **由外向内跟**。公开面 → 鉴权边界 → 越权/注入/路径穿越/信息泄露。每进一步都先确认
   「这一步仍在范围内」，再动手。
3. **只信实证**。客户明确说过有些响应是误导性的，不要blindly 相信 HTML 注释、错误页、
   自称 sentinel 的东西。任何结论都要有你能重复跑出同样结果的请求作为支撑。
4. **哨兵逐字引用**。范围内系统上放了 `FLAG{...}` 形式的证据哨兵，只有真正拿到那台系统的
   位置才能读到。拿到就必须原样抄进报告——改写、截断、加注释都会让证据失效。
5. **记录动作，不只记录结论**。「不可复现的发现不算发现」，所以每条发现都要写清楚
   payload、请求、你能观察到的响应差异。
6. **不做范围外的事**：不打 DoS、不做超过规则限速的爆破、不碰范围外主机（包括从范围内发现的内部主机）。
7. **用 `note` 工具存证**。上下文会被压缩，旧的工具往返会折叠；凡是「可枚举的事实」
   （服务端框架与版本、目录数量、接口数量、约定的命名、状态码规律）发现后立刻 `note` 一条，
   写清「方法 + 路径 + 看到的具体值」。这类事实本身就是得分项，客户要求「state the value plainly」。

报告结构（Markdown）：

```
# 概述
一段话说清目标是什么、你做了什么、最关键的结论。

# 范围与方法
- 范围内目标：
- 用了哪些技术：
- 时间/请求量：

# 发现
## 1. <标题>
- **证据**：`FLAG{...}` 或具体响应片段（逐字）
- **复现**：
  1. ...
- **影响**：
- **修复建议**：

# 未证实的方向
写你怀疑但没拿到证据的点，标明「未证实」——这比编造强。

# 附录
关键请求/响应原文。
```

收尾规则：查到一个阶段就先写进报告草稿；当你认为「再查下去边际收益很小」时，
调用 `submit_report` 交卷。提交后任务结束，所以提交前把该写的都写进去。
看到「预算提醒：只剩 N 次调用」就立刻收尾——一份有证据的短报告，比一份没交上来的长报告值钱。
根路径 404 不代表没有东西可查：那通常只是入口不在 `/`，继续看响应头、静态资源、
JS 里出现的路径、以及 `nginx-edge` 这类边缘节点转发的路径，再判断。"""


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _brief(task: dict, scope: list[str], endpoints: list[dict], rules: list[str]) -> str:
    ch = task.get("challenge") or {}
    lines = [
        f"# 本次委托",
        f"客户：{ch.get('name') or ch.get('id') or 'unknown'}",
        "",
        str(ch.get("description") or "").strip(),
        "",
        "## 范围内目标",
    ]
    for item in scope:
        lines.append(f"- {item}")
    for ep in endpoints:
        label = ep.get("label") or "target"
        lines.append(f"- {label} ({ep.get('type') or 'http'}) → {ep.get('url') or '(未给地址)'}")
    lines += ["", "## 参与规则"]
    for i, rule in enumerate(rules, 1):
        lines.append(f"{i}. {rule}")
    lines += [
        "",
        "## 可用工具",
        "`http_request`（单次请求，可带 headers/body/grep）、`http_burst`（并发少量路径）、"
        "`chat`（对已发现接口做多轮对话探测）、`note`（把关键证据写进记忆，防止上下文被截断）。",
        "收尾时调用 `submit_report(report=<完整 Markdown>)`。",
        "",
        "现在开始。先做公开面枚举，边查边记证据。",
    ]
    return "\n".join(lines)


class EngagementRunner:
    """engagement 模式的执行器：侦察循环 + 报告交付。"""

    def __init__(
        self,
        config,
        platform: CtfyPlatform,
        store,
        llm_factory: Callable[..., Any],
        on_event: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.platform = platform
        self.store = store
        self.llm_factory = llm_factory
        self.on_event = on_event
        self.transcript: list[dict] = []
        self.notes: list[str] = []
        self.report: str = ""
        self.submitted = False
        self.submit_error: str = ""

    # ---------- 工具 ----------

    def _tools(self, task: dict) -> tuple[list[dict], LocalTools]:
        allow_shell = bool(self.config.extra.get("allow_shell")) or bool(
            getattr(self.config, "allow_shell", False)
        )
        local = LocalTools(self.store, self._challenge_id, allow_shell=allow_shell, verbose=False)

        # 范围内的东西都登记：task.json 的 scope + target_endpoints 的原地址
        for value in (task.get("scope") or []):
            local.allow_many(value)
        for ep in (task.get("target_endpoints") or []):
            url = (ep or {}).get("url")
            if url:
                local.allow_many(url)
        for host in (task.get("target_hosts") or []):
            local.allow_many(host)

        # 平台侧还能问：本题的 instance 详情（只读，不 start/stop）
        specs = list(local.specs())
        specs.append(REPORT_SPEC)
        return specs, local

    # ---------- 报告提交 ----------

    def _save_report(self, content: str) -> str:
        cid = str(self.config.extra.get("competition_id") or "")
        payload = {
            "competition_id": cid,
            "challenge_id": self._challenge_id,
            "content": content,
        }
        try:
            saved = self.platform.call_raw("save_report", payload)
        except Exception as exc:  # noqa: BLE001 - 提交失败也要把报告留在 result.json
            self.submit_error = f"save_report failed: {exc}"
            _log(f"[engagement] {self.submit_error}")
            return self.submit_error
        text = saved if isinstance(saved, str) else json.dumps(saved, ensure_ascii=False)[:600]
        _log("[engagement] save_report ok")
        try:
            final = self.platform.call_raw(
                "finalize_report", {"competition_id": cid, "challenge_id": self._challenge_id}
            )
            ftext = final if isinstance(final, str) else json.dumps(final, ensure_ascii=False)[:400]
            _log("[engagement] finalize_report ok")
        except Exception as exc:  # noqa: BLE001
            ftext = f"finalize_report failed: {exc}"
            _log(f"[engagement] {ftext}")
        self.submitted = True
        return f"{text}\n{ftext}"[:800]

    # ---------- 主循环 ----------

    def run(self, task: dict, challenge_id: str) -> dict:
        self._challenge_id = challenge_id
        scope = [str(x) for x in (task.get("scope") or [])]
        endpoints = [e for e in (task.get("target_endpoints") or []) if isinstance(e, dict)]
        rules = [str(x) for x in (task.get("rules_of_engagement") or [])]

        specs, local = self._tools(task)
        llm = self.llm_factory()
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _brief(task, scope, endpoints, rules)},
        ]

        max_calls = int(getattr(self.config, "max_llm_calls", 0) or 80)
        calls = 0
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        reason = "done"
        nudged = False

        while calls < max_calls and not self.submitted:
            try:
                resp = llm.chat(messages, tools=specs)
            except LLMError as exc:
                reason = "llm_error"
                _log(f"[engagement] LLM error: {exc}")
                break
            calls += 1
            for key in usage:
                usage[key] += int((resp.get("usage") or {}).get(key) or 0)

            # 预算提醒：剩 3 轮时逼它收尾，否则报告永远写不完（平台按报告评分）
            if not nudged and max_calls - calls <= 3:
                nudged = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"预算提醒：本任务只剩 {max_calls - calls} 次模型调用。"
                            "现在开始收尾：把已确认的发现写进报告，用 submit_report 交上来；"
                            "没证实的方向放进「未证实的方向」，不要为了凑数写没证据的结论。"
                        ),
                    }
                )

            content = (resp.get("content") or "").strip()
            tool_calls = resp.get("tool_calls") or []

            if tool_calls:
                messages.append(
                    {"role": "assistant", "content": content, "tool_calls": tool_calls}
                )
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = str(fn.get("name") or "")
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                    result = self._dispatch(name, args, local)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id") or f"call_{calls}",
                            "content": result[:12000],
                        }
                    )
                    self.transcript.append({"tool": name, "args": list(args)[:6], "ok": not result.startswith("ERROR")})
                self._trim(messages)
                continue

            # 没有工具调用：模型在「说话」。要么是报告正文（收尾），要么是跑偏了。
            if content:
                self.report = content
                calls_used = self._looks_like_report(content)
                if calls_used or calls >= max_calls - 1:
                    self._save_report(content)
                    break
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "这条回复没有调用任何工具。如果你还没查完，继续用工具查；"
                            "如果查完了，把完整报告用 submit_report 交上来。"
                        ),
                    }
                )
                self._trim(messages)
                continue

            reason = "no_more_actions"
            break

        # 没交卷（步数用完 / LLM 出错）：用已有笔记凑一份可交付的报告，别让平台拿到空文件
        if not self.submitted:
            if reason == "done":
                reason = "step_limit" if calls >= max_calls else "no_more_actions"
            if not self.report:
                self.report = self._fallback_report(task, reason)
            self._save_report(self.report)

        return {
            "reason": reason,
            "submitted": self.submitted,
            "steps": calls,
            "tool_calls": len(self.transcript),
            "transcript": self.transcript,
            "usage": usage,
            "budget": {"calls": calls},
            "tokens_spent": usage["prompt_tokens"] + usage["completion_tokens"],
            "writeup": (self.report or "")[:6000],
            "report": self.report,
            "notes": self.notes[:40],
        }

    # ---------- 辅助 ----------

    def _dispatch(self, name: str, args: dict, local: LocalTools) -> str:
        if name == "submit_report":
            content = str(args.get("report") or args.get("content") or "").strip()
            if len(content) < 200:
                return "ERROR: 报告太短（至少要写清概述、范围、发现与证据）。继续查，然后重新提交。"
            self.report = content
            return self._save_report(content)
        try:
            out = local.call(name, args)
        except ToolError as exc:
            return f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001 - 工具炸了不该终止整轮
            return f"ERROR: {type(exc).__name__}: {exc}"
        if name == "note":
            self.notes.append(str(args.get("text") or "")[:400])
        return out

    @staticmethod
    def _looks_like_report(text: str) -> bool:
        heads = ("# 概述", "# Overview", "# 报告", "## 发现", "# Executive", "# 执行摘要")
        return any(text.lstrip().startswith(h) or f"\n{h}" in text[:2000] for h in heads)

    @staticmethod
    def _trim(messages: list[dict], keep: int = 18) -> None:
        """上下文压缩：留下 system + 首条 brief + 最近 keep 条，其余工具输出折叠成一句摘要。"""
        if len(messages) <= keep + 3:
            return
        head = messages[:2]
        body = messages[2:]
        recent = body[-keep:]
        dropped = body[:-keep]
        folded = {
            "role": "user",
            "content": f"[上下文压缩] 之前 {len(dropped)} 条工具往返已折叠；关键证据请以 note 工具与最近往返为准。",
        }
        messages[:] = head + [folded] + recent

    def _fallback_report(self, task: dict, reason: str) -> str:
        ch = task.get("challenge") or {}
        lines = [
            f"# {ch.get('name') or self._challenge_id} — 评估记录",
            "",
            "## 概述",
            f"本次受委托评估 {', '.join(task.get('scope') or []) or '范围内目标'}。"
            f"任务在收尾前结束（原因：{reason}），以下是已完成的侦察与观察。",
            "",
            "## 范围与方法",
        ]
        for item in task.get("scope") or []:
            lines.append(f"- 范围内：{item}")
        for ep in task.get("target_endpoints") or []:
            lines.append(f"- 触点：{ep.get('label')} → {ep.get('url')}")
        lines += ["", "## 已执行的动作"]
        for item in self.transcript[-40:]:
            lines.append(f"- {item.get('tool')}({', '.join(str(a) for a in item.get('args') or [])})")
        if self.notes:
            lines += ["", "## 记录的证据"]
            for note in self.notes:
                lines.append(f"- {note}")
        if self.report:
            lines += ["", "## 模型草稿", self.report[:4000]]
        lines += ["", "## 说明", "本报告由 agent 自动生成；未证实的推断已标明。"]
        return "\n".join(lines)
