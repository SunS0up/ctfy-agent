#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A2 伴生文本契约：`PLAN:` / `REFLECTION:` 的确定性解析与停滞检测。

为什么不是「提示词里写一句就算完」
--------------------------------
Cybench 的四段自述（Reflection / Research Plan and Status / Thought / Log）里，
PLAN 靠模型**自觉原样复制**上轮计划来防漂移；一旦模型改写措辞或自家截断丢掉，
计划就失效了——它没有任何代码侧的消费者。

这里把 PLAN 变成**有状态**的：解析 → 存档 → 用确定性信号（账本里是否出现新的、
非重复的尝试）判断「这一步到底有没有推进」。同一份计划连续多步不动且没有新尝试，
就注入一次「换假设或收口」的提示。所以：

- 解析是纯函数 `parse_companion()`，任何模型措辞走样都不影响账本与去重；
- 停滞判定只看两个确定性量：`plan` 字符串是否变化、`ledger` 新增尝试条数是否 > 0；
- 提示是**有界**的：缺标签每 run 最多提醒 `max_reminders` 次，停滞每个 episode 只提示一次，
  避免模型为凑格式反复烧步数（同 A2 证据契约的 `MAX_REPORT_REJECTIONS` 思路）。

刻意不做的事：不因为缺标签而拒绝/重跑工具调用（那会浪费步数），也不解析自由文本动作。
"""

from __future__ import annotations

import re

PLAN = "PLAN"
REFLECTION = "REFLECTION"

# 标签行：允许 Markdown 强调符（冒号前后都可能出现）、列表符号、中英文冒号、中文标签名
_LABEL_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|[#>]+[ \t]*|[0-9]+[.)][ \t]+)?"
    r"[*_`]{0,2}(PLAN|REFLECTION|计划|反思)[*_`]{0,2}[ \t]*[:：][ \t]*[*_`]{0,2}[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

_CAP = 300

_MISSING_HINT = (
    "[格式] 这次回复没写 PLAN: / REFLECTION: 标签。请在下一次回复里、调用工具之前补上两行：\n"
    "PLAN: <当前计划，一句话；计划没变就原样复制上一轮>\n"
    "REFLECTION: <上一步结果说明什么、下一步为什么这么做>"
)

_STALL_HINT = (
    "[停滞] 你的 PLAN 已连续 {rounds} 步没有变化，这几步也没有产生任何新的、非重复的尝试。\n"
    "当前 PLAN：{plan}\n"
    "同一计划继续执行只会重复同样的结果。二选一：换一个假设（新参数/新路径/新攻击面），"
    "或把这方向用 report 交成「已证伪」并说明卡点。"
)


def _squash(text: str) -> str:
    return " ".join(str(text or "").split())


def _clean(text: str) -> str:
    """压空白 + 去掉包住值的 Markdown 强调符（`**读 JS**` → `读 JS`）。"""
    return _squash(text).strip("*_`").strip()


def parse_companion(content: str) -> dict[str, str]:
    """从 assistant 回复文本里取 `PLAN:` / `REFLECTION:` 两段（缺则为空串）。

    宽容处理：`**PLAN:**`、`PLAN：`（全角）、`1. PLAN:`、`- 计划：`；
    值可跨多行，直到下一个标签行为止。取不到就返回空串，绝不抛异常——
    这一步在每轮循环里跑，任何异常都会毁掉整道题。
    """
    text = str(content or "")
    marks = list(_LABEL_RE.finditer(text))
    out = {PLAN: "", REFLECTION: ""}
    for index, match in enumerate(marks):
        raw_label = (match.group(1) or "").upper()
        key = REFLECTION if raw_label in ("REFLECTION", "反思") else PLAN
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = _clean(text[match.start(2):end])
        if body and not out[key]:
            out[key] = body
    return out


class PlanTrack:
    """一个角色（单 agent / 专家 / 协调者）的计划状态机。

    只回答两个问题：PLAN 变了没有？这一步推进了没有？以及据此判断是否停滞。
    """

    def __init__(self, stall_after: int = 3, max_reminders: int = 2, cap: int = _CAP):
        self.plan = ""
        self.reflection = ""
        self.plan_changes = 0      # PLAN 改写次数（漂移/演进的可观测指标）
        self.missing_steps = 0     # 没写 PLAN 的步数（格式遵从度）
        self.reminders = 0         # 已发出的格式提醒数
        self.stall_rounds = 0      # 当前连续「计划不变 + 无新尝试」步数
        self.stall_events = 0      # 已发出的停滞提示次数
        self.stall_after = max(2, int(stall_after))
        self.max_reminders = max(0, int(max_reminders))
        self.cap = max(40, int(cap))
        self._armed = True         # 每个停滞 episode 只提示一次

    def observe(self, content: str, *, progressed: bool) -> dict:
        """登记一步回复。

        `progressed` 必须来自确定性信号（账本新增尝试数 > 0），不能靠模型自述。
        """
        parsed = parse_companion(content)
        plan = parsed[PLAN][: self.cap]
        reflection = parsed[REFLECTION][: self.cap]
        if reflection:
            self.reflection = reflection

        changed = bool(plan) and plan != self.plan
        if changed:
            self.plan = plan
            self.plan_changes += 1

        missing = not plan
        if missing:
            self.missing_steps += 1

        # 计划变了 或 真的推进了 → 不算停滞，并重新武装提示（新一轮停滞还能再提醒一次）
        if changed or progressed:
            self.stall_rounds = 0
            self._armed = True
        else:
            self.stall_rounds += 1

        return {
            "plan": plan,
            "reflection": reflection,
            "changed": changed,
            "missing": missing,
            "stall_rounds": self.stall_rounds,
        }

    def reminder(self) -> str:
        """缺标签时的格式提醒（有配额，用完就闭嘴）。"""
        if self.reminders >= self.max_reminders:
            return ""
        self.reminders += 1
        return _MISSING_HINT

    def stalled(self) -> bool:
        return bool(self.plan) and self._armed and self.stall_rounds >= self.stall_after

    def stall_hint(self) -> str:
        if not self.stalled():
            return ""
        self._armed = False
        self.stall_events += 1
        return _STALL_HINT.format(rounds=self.stall_rounds, plan=self.plan[:160])

    def stats(self) -> dict:
        return {
            "plan": self.plan,
            "plan_changes": self.plan_changes,
            "missing_steps": self.missing_steps,
            "reminders": self.reminders,
            "stall_rounds": self.stall_rounds,
            "stall_events": self.stall_events,
        }
