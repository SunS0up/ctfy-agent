#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断点续跑（P0-1）：协调者主循环的 checkpoint / resume。

为什么值得做
------------
一次跑题 = 80 次调用 / 800k token ≈ 几十分钟。协调者的 `messages` 是本 run 最贵的状态，
进程一被杀（会话切换 / OOM / 平台重启）就全部清零——003 探针实测在 135k token 处被杀、
一道题前功尽弃。BoxPwnr 等开源框架都有全量状态持久化 + resume，我们没有。

设计（刻意收敛）
----------------
- 只负责「存 / 取」两个纯函数；「要不要续跑」的策略在 crew.py（它知道 status 与 extra.resume）。
- 快照对象：messages（协调者完整上下文）、reports（已交回专家结论）、ledger（跨 delegate 去重账本）、
  plan（停滞检测状态机）。这些都是纯 JSON 可序列化对象。
- 写盘用 tmp + os.replace 原子替换，被 kill 也不会留下半截文件。
- 读回失败（缺文件 / 损坏 / 题号不符 / 未来字段漂移）一律返回 None，绝不抛——调用方当无 checkpoint 处理。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .companion import PlanTrack
from .ledger import Hypothesis, Ledger, Observation, Tried

VERSION = 1


# ---------- Ledger 序列化 ----------

def ledger_to_dict(ledger: Ledger) -> dict:
    """账本全量快照。`_newly_refuted` 是瞬态（每次注入即 drain），不落盘。"""
    return {
        "tried": {key: asdict(value) for key, value in ledger.tried.items()},
        "order": [asdict(value) for value in ledger.order],
        "hypotheses": {key: asdict(value) for key, value in ledger.hypotheses.items()},
        "observations": [asdict(value) for value in ledger.observations],
        "duplicates": ledger.duplicates,
        "duplicate_of": dict(ledger.duplicate_of),
        "refute_after": ledger.refute_after,
        "max_tried": ledger.max_tried,
        "max_observations": ledger.max_observations,
        "raw_window": ledger.raw_window,
        "seq": ledger._seq,
    }


def ledger_from_dict(data: dict | None) -> Ledger | None:
    """反序列化账本。非 dict / 字段漂移 / 手改脏数据 → None（调用方退回空账本），绝不抛。"""
    if not isinstance(data, dict):
        return None
    try:
        ledger = Ledger(
            refute_after=int(data.get("refute_after") or 12),
            max_tried=int(data.get("max_tried") or 400),
            max_observations=int(data.get("max_observations") or 60),
            raw_window=int(data.get("raw_window") or 60),
        )
        ledger.tried = {key: Tried(**value) for key, value in (data.get("tried") or {}).items()}
        ledger.order = [Tried(**value) for value in (data.get("order") or [])]
        ledger.hypotheses = {key: Hypothesis(**value) for key, value in (data.get("hypotheses") or {}).items()}
        ledger.observations = [Observation(**value) for value in (data.get("observations") or [])]
        ledger.duplicates = int(data.get("duplicates") or 0)
        ledger.duplicate_of = {key: int(value) for key, value in (data.get("duplicate_of") or {}).items()}
        ledger._seq = int(data.get("seq") or 0)
        return ledger
    except (TypeError, ValueError, KeyError, AttributeError):
        # 字段漂移 / 手改脏数据：退回空账本（宁可少拦，也别崩 run）
        return None


# ---------- PlanTrack 序列化 ----------

_PLAN_FIELDS = (
    "plan", "reflection", "plan_changes", "missing_steps", "reminders",
    "stall_rounds", "stall_events", "stall_after", "max_reminders", "cap", "_armed",
)


def plan_to_dict(plan: PlanTrack) -> dict:
    return {key: getattr(plan, key) for key in _PLAN_FIELDS}


def plan_from_dict(data: dict | None) -> PlanTrack | None:
    if not isinstance(data, dict):
        return None
    try:
        plan = PlanTrack(
            stall_after=int(data.get("stall_after") or 3),
            max_reminders=int(data.get("max_reminders") or 2),
            cap=int(data.get("cap") or 300),
        )
        plan.plan = str(data.get("plan") or "")
        plan.reflection = str(data.get("reflection") or "")
        for key in ("plan_changes", "missing_steps", "reminders", "stall_rounds", "stall_events"):
            setattr(plan, key, int(data.get(key) or 0))
        plan._armed = bool(data.get("_armed", True))
        return plan
    except (TypeError, ValueError, AttributeError):
        return None


# ---------- checkpoint 读写 ----------

def save(path: Path, *, challenge_id: str, next_round: int, messages: list, reports: list,
         ledger: Ledger, plan: PlanTrack) -> None:
    payload = {
        "version": VERSION,
        "challenge_id": challenge_id,
        "next_round": int(next_round),
        "messages": messages,
        "reports": reports,
        "ledger": ledger_to_dict(ledger),
        "plan": plan_to_dict(plan),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load(path: Path, *, challenge_id: str) -> dict | None:
    """读回 checkpoint。缺文件 / 损坏 / 题号不符 / 版本不符 / 字段类型不对 → None。

    不校验就 return 会把坏数据一路带进 crew.solve 的恢复块（int("abc") 之类直接崩掉整道题），
    所以这里把关：坏 checkpoint 一律当不存在，绝不抛、绝不让坏字段流出。
    """
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != VERSION:
        return None
    if data.get("challenge_id") != challenge_id:
        return None
    # 恢复块会直接 int(next_round) / list(messages) / list(reports)：先验类型，挡住 ValueError 与垃圾数据
    if not isinstance(data.get("next_round"), int):
        return None
    if not isinstance(data.get("messages"), list):
        return None
    if not isinstance(data.get("reports"), list):
        return None
    return data


def discard(path: Path) -> None:
    """正常收尾时删掉 checkpoint，避免下一跑把「已完成的 run」误当崩溃残留而续跑。"""
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
