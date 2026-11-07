#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每道题的状态机 + 落盘。

设计目标：进程重启后能接着跑；提交过的东西绝不重复提交。
状态：new → parsed → instance_up → probing → candidate_flag → submitted → done
                                      ↘ stalled（超时/超预算，放弃但留记录）
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import msvcrt
except ImportError:  # pragma: no cover - 非 Windows
    msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


@contextlib.contextmanager
def _lock_exclusive(path: Path):
    """进程间排他文件锁（Windows msvcrt / POSIX fcntl），保护提交幂等的读-改-写。"""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        if msvcrt is not None:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        elif fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if msvcrt is not None:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

STATUSES = (
    "new",
    "parsed",
    "instance_up",
    "probing",
    "candidate_flag",
    "submitted",
    "done",
    "stalled",
)

# 允许的状态迁移（宽松：只挡明显倒退和重复提交）
ALLOWED = {
    "new": {"new", "parsed", "instance_up", "stalled"},
    "parsed": {"parsed", "instance_up", "probing", "stalled"},
    "instance_up": {"instance_up", "probing", "candidate_flag", "stalled"},
    "probing": {"probing", "candidate_flag", "instance_up", "stalled"},
    "candidate_flag": {"candidate_flag", "submitted", "probing", "stalled"},
    "submitted": {"submitted", "done", "probing"},
    "done": {"done"},
    "stalled": {"stalled", "probing", "parsed", "instance_up"},
}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def flag_hash(flag: str) -> str:
    return hashlib.sha256(flag.strip().encode("utf-8")).hexdigest()[:16]


_TAG_SPLIT = re.compile(r"[,;/|\s]+")


def normalize_tags(value) -> list[str]:
    """把平台各种形态的 tags（list / 逗号分隔字符串 / 单值 / dict）归一成去重保序的 list[str]。

    平台免费给的题型先验（tags/difficulty/solves_count）是解题最重要的线索，
    入库时宁可宽容解析，也不要静默丢掉。
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw: list = _TAG_SPLIT.split(value)
    elif isinstance(value, dict):
        for key in ("tags", "tag", "labels", "topics", "name"):
            if value.get(key):
                return normalize_tags(value[key])
        raw = list(value.keys())
    elif isinstance(value, (list, tuple, set)):
        raw = []
        for item in value:
            if isinstance(item, str):
                raw.extend(_TAG_SPLIT.split(item))
            elif isinstance(item, (list, tuple, set, dict)):
                raw.extend(normalize_tags(item))
            elif item is not None:
                raw.append(str(item))
    else:
        raw = [str(value)]
    out: list[str] = []
    for tag in raw:
        tag = str(tag).strip()
        if tag and tag not in out:
            out.append(tag)
    return out[:24]


def normalize_difficulty(value) -> str:
    """难度可能是 'easy' / 'hard' / 数字；统一成短字符串。"""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return ""
    return str(value).strip()[:32]


def normalize_solves(value) -> int | None:
    """已解队伍数；拿不到/解析不了就是 None（不写 0 以免伪造「无人解出」）。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


@dataclass
class Challenge:
    id: str
    name: str = ""
    category: str = ""
    points: int | None = None
    description: str = ""
    # 题型先验：平台 get_challenge / list_challenges 免费给，必须入库并送达给模型
    tags: list[str] = field(default_factory=list)   # 如 weak_password / osint / authentication_failures
    difficulty: str = ""                            # easy / medium / hard，也可能是数字
    solves_count: int | None = None                 # 已解队伍数（难度与方向的客观参照）
    status: str = "new"
    attempts: int = 0
    submitted_ok: bool = False
    instance: dict = field(default_factory=dict)
    instance_id: str = ""         # 平台实例 UUID（真实平台按它提交/停止）
    oracle_calls: int = 0         # verify_answer 探针次数（不记账的免费试答案）
    tokens: dict = field(default_factory=dict)  # 累计用量（跨进程继承，费用刹车用）
    flags_tried: list[str] = field(default_factory=list)  # flag 的 sha256 前缀
    flag_value: str = ""          # 只在校验通过后记录
    notes: list[str] = field(default_factory=list)
    tool_counts: dict = field(default_factory=dict)  # 写操作频控计数（start/stop 实例等）
    steps: int = 0
    updated_at: str = field(default_factory=now)

    def touch(self) -> None:
        self.updated_at = now()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Challenge":
        known = {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
        item = cls(**known)
        # 老 state.json 兼容 + 手写脏数据的宽容修复
        item.tags = normalize_tags(item.tags)
        item.difficulty = normalize_difficulty(item.difficulty)
        item.solves_count = normalize_solves(item.solves_count)
        return item

    def facets(self) -> str:
        """题型线索单行文本：`难度 easy | 已解 10 队 | tags: weak_password, osint`。

        brief 与专家任务头共用，保证「平台给了什么」在提示里一字不丢。
        """
        bits: list[str] = []
        if self.difficulty:
            bits.append(f"难度 {self.difficulty}")
        if self.solves_count is not None:
            bits.append(f"已解 {self.solves_count} 队")
        if self.tags:
            bits.append("tags: " + ", ".join(self.tags))
        return " | ".join(bits)


class Store:
    """一个 run 一个 state.json，原子写（tmp + replace），崩溃不留半截文件。"""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "state.json"
        self.challenges: dict[str, Challenge] = {}
        self.meta: dict = {}

    # ---------- 读写 ----------

    def load(self) -> "Store":
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            self.meta = data.get("meta") or {}
            self.challenges = {
                key: Challenge.from_dict(value) for key, value in (data.get("challenges") or {}).items()
            }
        return self

    def save(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": {**self.meta, "saved_at": now()},
            "challenges": {key: value.to_dict() for key, value in self.challenges.items()},
        }
        tmp = self.path.with_suffix(f".json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- 题目 ----------

    def get(self, challenge_id: str) -> Challenge | None:
        return self.challenges.get(challenge_id)

    def require(self, challenge_id: str) -> Challenge:
        item = self.get(challenge_id)
        if item is None:
            raise KeyError(f"state 里没有题目 {challenge_id}，先跑：python -m ctfy_agent challenges --sync")
        return item

    def upsert(self, data: dict) -> Challenge:
        challenge_id = str(data.get("id") or data.get("challenge_id") or data.get("name"))
        item = self.challenges.get(challenge_id) or Challenge(id=challenge_id)
        for field_name in ("name", "category", "points", "description"):
            value = data.get(field_name)
            if value not in (None, ""):
                setattr(item, field_name, value)
        # 题型先验：tags 是列表，用并集合并（不同来源可能各给一部分，一次给不全也不丢）
        tags = normalize_tags(data.get("tags"))
        if tags:
            item.tags = list(dict.fromkeys(normalize_tags(item.tags) + tags))
        difficulty = normalize_difficulty(data.get("difficulty"))
        if difficulty:
            item.difficulty = difficulty
        solves = normalize_solves(data.get("solves_count"))
        if solves is not None:
            item.solves_count = solves
        item.touch()
        self.challenges[challenge_id] = item
        return item

    def set_status(self, challenge_id: str, status: str) -> Challenge:
        if status not in STATUSES:
            raise ValueError(f"未知状态 {status}，可选：{', '.join(STATUSES)}")
        item = self.require(challenge_id)
        if status not in ALLOWED.get(item.status, set()):
            raise ValueError(f"{challenge_id}: 不允许从 {item.status} 直接跳到 {status}")
        item.status = status
        item.touch()
        return item

    # ---------- flag 幂等（进程间原子） ----------

    def _lock_path(self) -> Path:
        return self.run_dir / "state.lock"

    def _refresh_flags(self, challenge_id: str) -> None:
        """锁内调用：把磁盘上其他进程写入的 flags_tried / attempts 合并进内存。"""
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        disk = (data.get("challenges") or {}).get(challenge_id)
        if not isinstance(disk, dict):
            return
        item = self.challenges.get(challenge_id)
        if item is None:
            self.challenges[challenge_id] = Challenge.from_dict(disk)
            return
        merged = list(dict.fromkeys(list(item.flags_tried) + list(disk.get("flags_tried") or [])))
        item.flags_tried = merged
        item.attempts = max(item.attempts, int(disk.get("attempts") or 0))
        if disk.get("submitted_ok"):
            item.submitted_ok = True

    def reserve_submit(self, challenge_id: str, flag: str, limit: int) -> str:
        """原子地预留一次提交（并发下同一条 flag 只会成功预留一次）。

        返回 'ok' / 'duplicate' / 'limit'。预留成功后必须调用 mark_submitted 回写结果。
        """
        with _lock_exclusive(self._lock_path()):
            self._refresh_flags(challenge_id)
            item = self.require(challenge_id)
            digest = flag_hash(flag)
            if digest in item.flags_tried:
                return "duplicate"
            if item.attempts >= limit:
                return "limit"
            item.flags_tried.append(digest)
            item.attempts += 1
            if item.status == "probing":
                item.status = "candidate_flag"
            item.touch()
            self.save()
        return "ok"

    def mark_submitted(self, challenge_id: str, flag: str, *, accepted: bool) -> None:
        """提交结果出来后回写通过状态（不回退 attempts / flags_tried）。"""
        if not accepted:
            return
        with _lock_exclusive(self._lock_path()):
            item = self.require(challenge_id)
            item.submitted_ok = True
            item.flag_value = flag
            item.status = "submitted"
            item.touch()
            self.save()

    def can_submit(self, challenge_id: str, limit: int) -> bool:
        return self.require(challenge_id).attempts < limit

    # ---------- 写操作频控 ----------

    def record_call(self, challenge_id: str, name: str, limit: int) -> bool:
        """登记一次写操作；返回 False 表示已达上限。"""
        item = self.require(challenge_id)
        count = item.tool_counts.get(name, 0)
        if count >= limit:
            return False
        item.tool_counts[name] = count + 1
        item.touch()
        return True

    def bump_oracle(self, challenge_id: str, limit: int) -> bool:
        """登记一次 oracle 探针；返回 False 表示已达上限。"""
        item = self.require(challenge_id)
        if item.oracle_calls >= limit:
            return False
        item.oracle_calls += 1
        item.touch()
        return True

    def set_tokens(self, challenge_id: str, tokens: dict) -> None:
        """落盘累计用量（费用刹车要跨进程累计，不能只存在内存里）。"""
        item = self.require(challenge_id)
        item.tokens = dict(tokens or {})
        item.touch()

    # ---------- 笔记 / 转录 ----------

    def add_note(self, challenge_id: str, text: str) -> None:
        item = self.require(challenge_id)
        item.notes.append(f"[{now()}] {text[:2000]}")
        del item.notes[:-50]  # 只保留最近 50 条，防无限膨胀
        item.touch()

    def transcript_path(self, challenge_id: str) -> Path:
        target = self.run_dir / "transcripts"
        target.mkdir(parents=True, exist_ok=True)
        safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in challenge_id)
        return target / f"{safe}.jsonl"

    def append_transcript(self, challenge_id: str, record: dict) -> None:
        path = self.transcript_path(challenge_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": now(), **record}, ensure_ascii=False) + "\n")
