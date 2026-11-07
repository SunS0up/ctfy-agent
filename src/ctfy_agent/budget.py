#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""费用/用量上限：防止模型跑飞烧钱。

三层刹车，任何一层触发都在**下一次调用模型之前**抛 BudgetExceeded：
  - token 累计上限（按题落盘，重跑同一题会接着算，不能靠重跑绕过）
  - LLM 调用次数上限
  - 金额上限（需先配单价；不配单价就只按 token 卡）

为什么把闸门放在「调用之前」：一次 chat 花多少由模型决定，只有调用后才知道。
所以在请求发出前检查已有用量，代价是最多多花一次调用的量（有界），
换来的是无论模型多能刷，都不可能无限烧钱。
"""

from __future__ import annotations


class BudgetExceeded(RuntimeError):
    """预算耗尽。带上一份快照，方便落盘和汇报。"""

    def __init__(self, reason: str, snapshot: dict):
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot


class Budget:
    def __init__(
        self,
        *,
        max_tokens: int = 0,
        max_calls: int = 0,
        max_cost: float = 0.0,
        price_input: float = 0.0,
        price_output: float = 0.0,
        label: str = "",
    ):
        self.max_tokens = max(0, int(max_tokens or 0))       # 0 = 不限
        self.max_calls = max(0, int(max_calls or 0))         # 0 = 不限
        self.max_cost = max(0.0, float(max_cost or 0.0))     # 0 = 不限
        self.price_input = max(0.0, float(price_input or 0.0))    # 每百万 token 单价
        self.price_output = max(0.0, float(price_output or 0.0))
        self.label = label
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    # ---------- 读数 ----------

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost(self) -> float:
        return (
            self.prompt_tokens / 1_000_000 * self.price_input
            + self.completion_tokens / 1_000_000 * self.price_output
        )

    @property
    def tokens_left(self) -> int | None:
        return None if not self.max_tokens else max(0, self.max_tokens - self.total_tokens)

    @property
    def cost_left(self) -> float | None:
        return None if self.max_cost <= 0 else max(0.0, self.max_cost - self.cost)

    # ---------- 续算（跨进程继承历史用量） ----------

    def load(self, snapshot: dict | None) -> "Budget":
        snapshot = snapshot or {}
        self.prompt_tokens = max(self.prompt_tokens, int(snapshot.get("prompt_tokens") or 0))
        self.completion_tokens = max(self.completion_tokens, int(snapshot.get("completion_tokens") or 0))
        self.calls = max(self.calls, int(snapshot.get("calls") or 0))
        return self

    def snapshot(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "cost": round(self.cost, 6),
            "max_tokens": self.max_tokens,
            "max_calls": self.max_calls,
            "max_cost": self.max_cost,
        }

    # ---------- 闸门 ----------

    def exceeded(self) -> str | None:
        """超限原因；未超返回 None。"""
        if self.max_calls and self.calls >= self.max_calls:
            return f"模型调用次数已达上限 {self.max_calls} 次"
        if self.max_tokens and self.total_tokens >= self.max_tokens:
            return f"token 用量已达上限 {self.max_tokens}（当前 {self.total_tokens}）"
        if self.max_cost > 0 and self.cost >= self.max_cost:
            return f"费用已达上限 {self.max_cost:.4f}（当前 {self.cost:.4f}）"
        return None

    def check(self) -> None:
        reason = self.exceeded()
        if reason:
            raise BudgetExceeded(f"{self.label + '：' if self.label else ''}{reason}", self.snapshot())

    def record(self, usage: dict | None) -> None:
        usage = usage or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.calls += 1

    # ---------- 展示 ----------

    def describe(self) -> str:
        def cap(limit, unit=""):
            return f"{limit}{unit}" if limit else "不限"

        money = f"{self.cost:.4f} / {cap(self.max_cost)}" if (self.max_cost or self.price_input or self.price_output) else "未计价"
        return (
            f"token {self.total_tokens} / {cap(self.max_tokens)}  "
            f"调用 {self.calls} / {cap(self.max_calls)}  "
            f"费用 {money}"
        )
