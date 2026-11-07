#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性 Ledger：跨 delegate 共享的「试过什么 / 哪些方向被证伪 / 看到了什么」账本。

**为什么不用提示词让模型自己维护**（Cybench 的 `Log` 做法）：模型会漏抄、会被自家
截断丢掉、跨 delegate 直接清零。这里由代码在每次工具调用后写入，下一次注入提示的是
账本的渲染结果，而不是模型的自述。

三个对象：
  - `Tried`      一次已执行的调用。**去重键含归一化后的 args**（上一轮 50 万 token 的
                 根因就是只记结果不记参数，「已失败尝试勿重复」没有内容可依）。
  - `Hypothesis` 一个策略簇（如 `http POST /api/login`）的状态机 open → refuted。
                 同一簇内不同候选连续失败到阈值即证伪，用于定向注入「禁止再试」。
  - `Observation` 逐字证据（A2 的 report.evidence 会写进这里；本层先自动收录 flag 形态）。

设计边界（重要）：
  - **只拦截完全相同的调用**（同 tool + 同归一化 args）——同参数重试必然同结果，拦截无损。
  - **簇被证伪不硬拦**：tags 证明方向可能对（如 weak_password），只是候选没覆盖。
    证伪只做定向提示，交给模型决定换维度还是继续。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from json import JSONDecodeError
from urllib.parse import parse_qsl, urlencode, urlsplit

from .state import now

# 这些工具不参与去重拦截：note 是记账、report 是交结论（调用即结束回合）
NO_DEDUPE = frozenset({"note", "report"})

# 判定「这次调用没拿到新信息」的失败标记（确定性文本匹配，不做语义猜测）
FAILURE_MARKERS = (
    "401", "403", "404", "405", "422", "500",
    "invalid", "incorrect", "denied", "forbidden", "unauthorized",
    "not found", "no such", "error", "failed", "failure", "wrong", "expired",
    "拒绝", "失败", "错误", "不存在", "无效",
)

FLAG_RE = re.compile(r"[A-Za-z0-9_]{2,}\{[^}\s]{4,}\}")

# 「声称有发现」的强断言词（M4：这类结论必须能溯源到逐字证据）。
# 只收强断言词，避免把「没找到 / 都是 401」这类否定结论也要求证据（失败题无证据属正常）。
CLAIM_MARKERS = (
    "发现", "找到", "拿到", "取得", "绕过", "泄露", "成功登录", "登录成功", "凭证有效", "密码是", "口令是",
    "found", "obtained", "bypassed", "leaked", "logged in", "valid credential",
)

# 否定式前缀：先摘掉再看断言词，否则「未拿到凭据」「没找到」会被误判成「声称有发现」
_NEG_CLAIM_RE = re.compile(
    r"(未|没|没有|无|未能|无法|not|no|never|failed to|unable to|did not|didn't|couldn't)\s*"
    r"(找到|发现|拿到|取得|绕过|泄露|获得|确认|found|obtained|bypassed|leaked|confirmed)",
    re.IGNORECASE,
)

# 逐字证据校验时保留的原始输出上限（每个 Tried 一份）
RAW_CAP = 4000

_MAX_LABEL = 200


# ---------- 归一化 ----------


def _norm_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def normalize_url(url: str) -> str:
    """scheme://host[:port]/path?排序后的 query —— 主机小写、去默认端口、query 排序。

    只抹掉「字符层面的写法差异」，不改变语义：不同 path/不同 query 值仍是不同键。
    """
    raw = _norm_scalar(url).strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        # 端口越界/非数字：视为无端口。这种 URL 本就会被 _check_target 拒绝，
        # 归一化只负责降级不崩，别让一个坏 URL 击穿整个 run（审计 #1）。
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path or "/"
    return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")


def normalize_body(body) -> str:
    """请求体归一：JSON 对象按键排序（`{u,p}` 与 `{p,u}` 视为同一次尝试），否则压空白。"""
    if body in (None, ""):
        return ""
    if isinstance(body, (dict, list)):
        return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    text = _norm_scalar(body)
    try:
        parsed = json.loads(text)
    except (JSONDecodeError, ValueError):
        return " ".join(text.split())
    if isinstance(parsed, (dict, list)):
        return json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return " ".join(text.split())


def normalize_args(tool: str, args: dict | None) -> dict:
    """按工具语义抽出「决定结果」的参数，丢弃无关噪声（headers/timeout 等）。"""
    args = args or {}
    if tool == "http_request":
        method = (_norm_scalar(args.get("method")) or "GET").upper()
        # grep/offset/limit 是「看哪一段」的投影：同投影重复读才算重复
        out = {"method": method, "url": normalize_url(args.get("url"))}
        body = normalize_body(args.get("body") or args.get("data") or args.get("body_b64"))
        if body:
            out["body"] = body
        for key in ("grep", "offset", "limit"):
            if args.get(key) not in (None, ""):
                out[key] = _norm_scalar(args.get(key))
        # diff 改变「输出内容」（差异 vs 全窗），必须进键：否则 diff 调用与普通同参 GET 撞键，
        # 落账后反手把后续普通读也误拦（审计 B2）。
        if args.get("diff") in (True, "true", "1", "yes", "on"):
            out["diff"] = True
        # follow_redirect=false 返回 3xx+Location，与跟随后的响应完全不同，同样必须进键。
        # 语义与 tools_local.as_bool 对齐：缺省 True，只有「非真值」才改变行为（NoRedirect）。
        # 只写死 "false"/"0" 会漏掉 ""、"n" 等 as_bool 判 False 的值，导致与普通 GET 撞键误拦。
        follow = args.get("follow_redirect")
        if follow is not None:
            if isinstance(follow, bool):
                follows = follow
            elif isinstance(follow, (int, float)):
                follows = bool(follow)
            else:
                follows = str(follow).strip().lower() in ("1", "true", "yes", "y", "on")
            if not follows:
                out["follow_redirect"] = False
        return out
    if tool == "shell":
        return {"command": " ".join(_norm_scalar(args.get("command")).split())}
    if tool == "delegate":
        # 派活去重：同 role + 同 objective = 同一份侦察的重写（实测浪费 2/3 预算）。
        # objective 为主；task 是 A2 之前的旧字段，兼容保留（crew.py 已把 task 折进 objective）。
        objective = " ".join(_norm_scalar(args.get("objective") or args.get("task") or "").split())
        return {"role": _norm_scalar(args.get("role")), "objective": objective}
    if tool == "jwt_forge":
        # secret/token 绝不进明文：label 会经 ledger.digest() → 下一轮 task 流进别的角色上下文
        # （拦截消息 solver.py:247 也打印 label）。只存 sha1 前 8 位：同 secret 仍能去重，明文不出账本。
        return {
            "alg": (_norm_scalar(args.get("alg")) or "HS256").upper(),
            "secret_fp": hashlib.sha1(_norm_scalar(args.get("secret") or "").encode("utf-8")).hexdigest()[:8],
            "token_fp": hashlib.sha1(_norm_scalar(args.get("token") or "").encode("utf-8")).hexdigest()[:8],
            "claims": normalize_body(args.get("claims")),
            "header": normalize_body(args.get("header")),
            "remove": [str(x) for x in (args.get("remove") or [])] if isinstance(args.get("remove"), list) else [],
        }
    return {key: normalize_body(value) if isinstance(value, (dict, list)) else _norm_scalar(value)
            for key, value in args.items() if key not in ("headers", "timeout", "verify")}


def attempt_key(tool: str, args: dict | None) -> str:
    return f"{tool}|" + json.dumps(normalize_args(tool, args), sort_keys=True, ensure_ascii=False)


def attempt_label(tool: str, args: dict | None) -> str:
    """人读的调用摘要（凭据、路径都在里面，M2 的取证就看这个）。"""
    norm = normalize_args(tool, args)
    if tool == "http_request":
        head = f"http {norm.get('method', 'GET')} {norm.get('url', '')}"
        if norm.get("body"):
            head += f" body={norm['body']}"
        for key in ("grep", "offset", "limit"):
            if key in norm:
                head += f" {key}={norm[key]}"
        return head[:_MAX_LABEL]
    if tool == "shell":
        return f"shell {norm.get('command', '')}"[:_MAX_LABEL]
    if tool == "delegate":
        return f"delegate {norm.get('role', '')} {norm.get('objective', '')}"[:_MAX_LABEL]
    if tool == "jwt_forge":
        parts = ["jwt", norm.get("alg", "HS256")]
        if norm.get("secret_fp"):
            parts.append(f"secret=#{norm['secret_fp']}")
        if norm.get("token_fp"):
            parts.append("rewrite")
        return (" ".join(parts))[:_MAX_LABEL]
    return f"{tool} {json.dumps(norm, ensure_ascii=False, sort_keys=True)}"[:_MAX_LABEL]


def cluster_of(tool: str, args: dict | None) -> str:
    """策略簇签名：忽略具体候选值，只保留「方向」（方法+路径 / 工具 / 命令首词）。"""
    norm = normalize_args(tool, args)
    if tool == "http_request":
        try:
            path = urlsplit(norm.get("url", "")).path or "/"
        except ValueError:
            path = "/"
        return f"http {norm.get('method', 'GET')} {path}"
    if tool == "shell":
        words = str(norm.get("command", "")).split()
        return f"shell {words[0]}" if words else "shell"
    if tool == "delegate":
        return f"delegate {norm.get('role', '')}"
    return tool


_STATUS_RE = re.compile(r'"status"\s*:\s*(\d{3})')


def _http_status(text: str) -> int | None:
    """从 http_request 的结构化结果里读 status（{"status": 401, ...}）。读不到返回 None。

    转录/上下文里结果会被截断，`raw_decode` 因此常常失败——这时用正则兜一下，
    否则「200 + 大 JS 包」会被误判成失败（实测误判源）。
    """
    raw = _norm_scalar(text).strip()
    if not raw.startswith("{"):
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(raw)
    except (JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        status = data.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            return status
    match = _STATUS_RE.search(raw[:200])
    return int(match.group(1)) if match else None


def looks_failed(text: str, tool: str = "") -> bool:
    """这次调用是否「没拿到新信息」。

    http_request 优先看结构化 status（200 的 JS 包里出现 "error" 字样不算失败——
    实测按纯文本匹配会误判 4 次）；其它工具才退回关键字匹配。
    """
    if tool == "http_request":
        status = _http_status(text)
        if status is not None:
            return not (200 <= status < 400)
    lowered = _norm_scalar(text).lower()
    return any(marker in lowered for marker in FAILURE_MARKERS)


def one_line(text: str, limit: int = 160) -> str:
    return " ".join(_norm_scalar(text).split())[:limit]


def claims_discovery(text: str) -> bool:
    """这条结论是否在「声称有发现」（含 flag 形态或强断言动词，已排除否定式表述）。

    M4 只约束这类结论；否定型结论（「都是 401 / 没找到」）不要求证据。
    """
    raw = _NEG_CLAIM_RE.sub("", _norm_scalar(text))
    if FLAG_RE.search(raw):
        return True
    lowered = raw.lower()
    return any(marker in lowered for marker in CLAIM_MARKERS)


def _fold_ws(text: str) -> str:
    return " ".join(_norm_scalar(text).split())


# ---------- 数据对象 ----------


@dataclass
class Tried:
    seq: int
    key: str
    tool: str
    label: str
    cluster: str
    role: str = ""
    ok: bool = True
    digest: str = ""
    repeats: int = 1
    raw: str = ""   # 原始输出（截断到 RAW_CAP），供 report.evidence 做逐字子串校验


@dataclass
class Hypothesis:
    cluster: str
    status: str = "open"          # open / refuted
    attempts: int = 0             # 该方向不同候选数
    failures: int = 0             # 其中失败的不同候选数
    successes: int = 0
    last_failure: str = ""
    refuted_at: int | None = None


@dataclass
class Observation:
    text: str
    source: str
    seq: int
    at: str = field(default_factory=now)


# ---------- 账本 ----------


class Ledger:
    """一个 run 一本账（crew 里跨 delegate 共享；单 agent 里自己一本）。"""

    def __init__(self, *, refute_after: int = 12, max_tried: int = 400, max_observations: int = 60,
                 raw_window: int = 60):
        self.tried: dict[str, Tried] = {}
        self.order: list[Tried] = []
        self.hypotheses: dict[str, Hypothesis] = {}
        self.observations: list[Observation] = []
        self.duplicates = 0
        self.duplicate_of: dict[str, int] = {}
        self.refute_after = max(2, int(refute_after))
        self.max_tried = max(20, int(max_tried))
        self.max_observations = max(5, int(max_observations))
        self.raw_window = max(5, int(raw_window))
        self._newly_refuted: list[Hypothesis] = []
        self._seq = 0

    # ---- 构造 ----

    @classmethod
    def from_config(cls, config) -> "Ledger":
        """默认 refute_after=12：真实转录回放（runs/ledger_replay.py）显示 035 一次 run 内
        同一方向只有 18 个不同候选，阈值设 20 就永远不触发；12 能在浪费掉 2/3 候选后
        给出定向提示（只提示不硬拦，见模块文档）。"""
        extra = getattr(config, "extra", None) or {}
        return cls(
            refute_after=int(extra.get("ledger_refute_after") or 12),
            max_tried=int(extra.get("ledger_max_tried") or 400),
        )

    # ---- 尝试记录 ----

    def seen(self, tool: str, args: dict | None) -> Tried | None:
        """完全相同的调用（同 tool + 同归一化 args）已经执行过就返回它。"""
        return self.tried.get(attempt_key(tool, args))

    def note_duplicate(self, key: str) -> int:
        """记一次被拦下的重复调用，返回这是该键第几次重复。"""
        self.duplicates += 1
        count = self.duplicate_of.get(key, 0) + 1
        self.duplicate_of[key] = count
        hit = self.tried.get(key)
        if hit is not None:
            hit.repeats += 1
        return count

    def record(self, tool: str, args: dict | None, result: str, *, role: str = "", ok: bool | None = None) -> Tried:
        key = attempt_key(tool, args)
        existing = self.tried.get(key)
        if existing is not None:
            existing.repeats += 1
            return existing

        self._seq += 1
        failed = (not ok) if ok is not None else looks_failed(result, tool)
        item = Tried(
            seq=self._seq,
            key=key,
            tool=tool,
            label=attempt_label(tool, args),
            cluster=cluster_of(tool, args),
            role=role,
            ok=not failed,
            digest=one_line(result),
            raw=_norm_scalar(result)[:RAW_CAP],
        )
        self.tried[key] = item
        self.order.append(item)
        if len(self.order) > self.max_tried:
            for dropped in self.order[: -self.max_tried]:
                self.tried.pop(dropped.key, None)
            del self.order[: -self.max_tried]
        # 逐字校验只需要最近的输出：旧条目丢掉原文，省内存
        if len(self.order) > self.raw_window:
            for stale in self.order[: -self.raw_window]:
                stale.raw = ""

        self._touch_cluster(item)
        self._harvest_observation(item, result)
        return item

    def _touch_cluster(self, item: Tried) -> None:
        hyp = self.hypotheses.get(item.cluster)
        if hyp is None:
            hyp = Hypothesis(cluster=item.cluster)
            self.hypotheses[item.cluster] = hyp
        hyp.attempts += 1
        if item.ok:
            hyp.successes += 1
            return
        hyp.failures += 1
        hyp.last_failure = item.digest
        if hyp.status == "open" and hyp.failures >= self.refute_after and hyp.successes == 0:
            hyp.status = "refuted"
            hyp.refuted_at = self._seq
            self._newly_refuted.append(hyp)

    def _harvest_observation(self, item: Tried, result: str) -> None:
        """逐字收录证据形态（flag）。A2 起 report.evidence 也写进这里。"""
        for match in FLAG_RE.finditer(_norm_scalar(result)):
            self.observe(match.group(0), source=item.label, seq=item.seq)

    def observe(self, text: str, *, source: str = "", seq: int = 0) -> None:
        clean = " ".join(_norm_scalar(text).split())
        if not clean:
            return
        if any(existing.text == clean for existing in self.observations):
            return
        self.observations.append(Observation(text=clean[:500], source=source, seq=seq or self._seq))
        del self.observations[: -self.max_observations]

    def verify_evidence(self, evidence: str) -> str | None:
        """M4：evidence 必须是**某一次工具输出内的逐字连续子串**。

        命中则返回来源 label（可写进 observation 溯源），否则 None。
        只放宽空白折叠（模型复制大段 JSON 时常改缩进/换行），字符内容必须连续存在。
        """
        raw_evidence = _norm_scalar(evidence).strip()
        if len(raw_evidence) < 4:
            return None
        folded = _fold_ws(raw_evidence)
        for item in reversed(self.order):
            if not item.raw:
                continue
            if raw_evidence in item.raw:
                return item.label
            if folded and folded in _fold_ws(item.raw):
                return item.label
        return None

    # ---- 假设轮换 ----

    def refuted(self) -> list[Hypothesis]:
        return [hyp for hyp in self.hypotheses.values() if hyp.status == "refuted"]

    def open_hypotheses(self) -> list[Hypothesis]:
        return [hyp for hyp in self.hypotheses.values() if hyp.status == "open"]

    def drain_new_refutations(self) -> list[Hypothesis]:
        """取走「自上次注入以来新证伪的簇」——只在真的变了时注入，避免每步都喊。"""
        fresh = self._newly_refuted
        self._newly_refuted = []
        return fresh

    def recent_sources(self, k: int = 5, snippet: int = 120) -> str:
        """给「被拒的 report」回显可选证据来源：label + 真实输出片段（原样复制即可）。"""
        lines: list[str] = []
        for item in list(reversed(self.order)):
            if len(lines) >= k:
                break
            if not item.raw:
                continue
            lines.append(f"- {item.label} → {_fold_ws(item.raw)[:snippet]}")
        if not lines:
            return "可选来源：本回合还没有工具输出。"
        return "可选来源（最近的真实输出，可直接复制其中一段）：\n" + "\n".join(lines)

    def rotate(self, k: int = 3) -> str:
        """把最近证伪的方向渲染成「禁止再试」块（按失败次数取前 k）。"""
        refuted = sorted(self.refuted(), key=lambda hyp: (-hyp.failures, hyp.cluster))[:k]
        if not refuted:
            return ""
        lines = ["【假设轮换（确定性账本）】以下方向已被本题实测证伪，禁止再试同类参数："]
        for hyp in refuted:
            tail = f"（最近结果：{hyp.last_failure}）" if hyp.last_failure else ""
            lines.append(f"- {hyp.cluster}：{hyp.failures} 个不同候选全部失败{tail}")
        lines.append("同方向继续追加同类候选不算换方向；要么换信息维度，要么先写下新依据再动手。")
        return "\n".join(lines)

    # ---- 渲染 ----

    def digest(self, max_entries: int = 10, include_refuted: bool = True) -> str:
        """给模型看的紧凑账本：最近试过的调用（含参数）+ 被证伪的方向。"""
        if not self.order:
            return ""
        parts = ["【已试过（确定性账本，勿重复同一参数）】"]
        recent = self.order[-max_entries:]
        for item in reversed(recent):
            mark = "" if item.ok else "失败"
            repeats = f" ×{item.repeats}" if item.repeats > 1 else ""
            line = f"- #{item.seq} {item.label}{repeats}"
            if item.digest:
                line += f" → {item.digest[:110]}"
            elif mark:
                line += f" → {mark}"
            parts.append(line)
        if len(self.order) > len(recent):
            parts.append(f"- …另有 {len(self.order) - len(recent)} 次更早的尝试（同一参数不要重试）")
        if include_refuted:
            rotate = self.rotate()
            if rotate:
                parts.append("")
                parts.append(rotate)
        return "\n".join(parts)

    def render(self) -> str:
        """完整账本（落转录/事后取证用，不进上下文）。"""
        parts = [self.digest(max_entries=len(self.order) or 1)]
        if self.observations:
            parts.append("【证据（逐字）】")
            for obs in self.observations[-10:]:
                parts.append(f"- {obs.text}  ← {obs.source}")
        return "\n".join(part for part in parts if part)

    def stats(self) -> dict:
        """M2/M3 的验收数字：重复拦截数、不同尝试数、被证伪方向数、证据数。"""
        return {
            "attempts": len(self.order),
            "duplicates_intercepted": self.duplicates,
            "duplicate_keys": len(self.duplicate_of),
            "hypotheses": len(self.hypotheses),
            "refuted": len(self.refuted()),
            "open": len(self.open_hypotheses()),
            "observations": len(self.observations),
        }
