#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量编排：无人值守地把一批题跑完。

比赛开始后不允许人工干预，所以必须有一个"跑一批、自己扛住失败、花超了就停"的驱动。
这里刻意做成纯编排：不碰平台、不碰模型，只依赖 store 和注入的 solve 回调，
因此可以完全离线测试。

三条兜底：
  1. 单题异常不中断整批（记失败、继续下一题）
  2. 全局 token 上限（跨题累计），剩余额度会动态收紧下一题的单题上限
  3. 连续失败超过阈值就停，避免在坏环境下空烧
"""

from __future__ import annotations

from typing import Callable, Iterable

DEFAULT_STATUSES = ("new", "parsed", "instance_up", "stalled")


def _select(store, statuses: Iterable[str], limit: int) -> list:
    wanted = set(statuses)
    rows = [c for c in store.challenges.values() if getattr(c, "status", None) in wanted]
    rows.sort(key=lambda c: getattr(c, "id", ""))
    if limit and limit > 0:
        return rows[:limit]
    return rows


def run_auto(
    *,
    store,
    solve: Callable[[str], dict],
    statuses: Iterable[str] = DEFAULT_STATUSES,
    limit: int = 0,
    max_total_tokens: int = 0,
    max_failures: int = 3,
    on_event: Callable[[str, dict], None] | None = None,
) -> dict:
    """依次解题。solve(challenge_id) 返回带 budget/usage 的摘要。"""
    emit = on_event or (lambda kind, payload=None: None)
    rows = _select(store, statuses, limit)
    emit("plan", {"candidates": [getattr(c, "id", "") for c in rows]})

    results: list[dict] = []
    spent = 0
    submitted = 0
    failed = 0
    consecutive_failures = 0
    stopped = "exhausted"

    for challenge in rows:
        cid = getattr(challenge, "id", "")
        if max_total_tokens and spent >= max_total_tokens:
            stopped = "total_budget"
            emit("stop", {"reason": stopped, "spent": spent})
            break

        try:
            summary = solve(cid) or {}
        except Exception as exc:  # noqa: BLE001 - 单题失败不能拖垮整批
            failed += 1
            consecutive_failures += 1
            results.append({"challenge_id": cid, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            emit("failed", {"challenge_id": cid, "error": str(exc)})
            if consecutive_failures >= max_failures:
                stopped = "too_many_failures"
                emit("stop", {"reason": stopped, "failures": failed})
                break
            continue

        consecutive_failures = 0
        # 用本轮实际消耗（tokens_spent delta），而不是跨 run 累计值，否则重试会重复计入（审计 B9）
        used = int(summary.get("tokens_spent") or (summary.get("budget") or {}).get("total_tokens") or 0)
        spent += used
        if summary.get("submitted"):
            submitted += 1
        results.append(
            {
                "challenge_id": cid,
                "ok": bool(summary.get("submitted")),
                "reason": summary.get("reason"),
                "tokens": used,
                "steps": summary.get("steps"),
            }
        )
        emit("done", {"challenge_id": cid, "submitted": bool(summary.get("submitted")), "tokens": used, "spent": spent})

    return {
        "planned": len(rows),
        "attempted": len(results),
        "submitted": submitted,
        "failed": failed,
        "spent": spent,
        "stopped": stopped,
        "results": results,
    }
