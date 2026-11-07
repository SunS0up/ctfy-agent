#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文压缩：工具结果瘦身 + 关键事实常驻（M5 实测的成本杠杆）。

为什么需要（M5 真机数据）：
  两题成功的 run 分别是 135k / 210k token，其中 prompt 占 97%（completion 仅 2–5k）。
  也就是说钱几乎全花在「每一轮重新灌一遍历史」，而不是模型思考。历史里最肥的是
  http_request 的响应体：单条上限 6000 字符 ≈ 2000 token，12–23 次调用反复重发。

两个互补机制：
  1. shrink_tool_text：进上下文前把大响应压成「头 600 + 关键行 + 尾 200」（完整原文
     仍在转录里，模型可 grep/offset 再取）。关键行按 flag/token/session/状态码/接口 保留，
     避免把 flag 之类关键信息压掉。
  2. FactsBoard：从每次工具结果里抽取**结论级事实**（候选 flag、会话已建立、已访问端点、
     发现的 AI 工具名），渲染成一个 ≤520 字符的固定块，挂在第一条 user 消息里。
     滑窗压缩会丢历史，但事实块永不丢——这也是防盗：模型不会因为忘了「注册即登录」
     又重新注册一遍。

两个机制都只处理我方可确定的内容（工具返回 + 我方正则），不执行、不转述题目里的指令。
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

# 进上下文的工具结果目标长度（字符）。完整原文在转录 JSONL 里，模型可用 grep/offset 再取。
DEFAULT_TOOL_CHARS = 1600

# 关键行：命中这些词的行在瘦身时保留（flag/凭据/会话/接口/错误）
KEY_LINE_RE = re.compile(
    r"(?i)(flag\{|token|secret|passwd|password|session|set-cookie|authorization|"
    r"admin|internal|restricted|/api/|/kb/|/portal|/docs|traceback|error|403|401|500|"
    # 表单结构：压掉了模型就得反复 grep 表单字段（m5j 实测白烧 6 步 ≈ 48k token）
    r"<(?:input|textarea|select|option|button|form)|name=|type=.hidden|csrf)"
)

FLAG_RE = re.compile(r"FLAG\{[^}\s]{4,120}\}")
PATH_RE = re.compile(r"/[A-Za-z0-9_\-]{2,}(?:/[A-Za-z0-9_\-.]+)+")
HREF_RE = re.compile(r"""href=["'](/[^"']{2,80})["']""")
FIELD_RE = re.compile(r"""name=["']([^"']{1,40})["']""")
CRED_RE = re.compile(r"(?i)(?:authorization|set-cookie|x-api-key|bearer)\s*[:=]\s*[^\s\"'<>]{6,120}")
TOOL_NAME_RE = re.compile(r"@@TOOL:\s*([A-Za-z_][\w]*)")
TOOL_DEF_RE = re.compile(r"\b([a-z_]{3,32})\(([^)]{0,24})\)")
STATUS_RE = re.compile(r'"status"\s*:\s*(\d{3})')
TICKET_RE = re.compile(r"(?i)ticket\s*#?\s*(\d{1,3})")


def shrink_tool_text(text: str, limit: int = DEFAULT_TOOL_CHARS) -> str:
    """把过长的工具结果压成「头 + 关键行 + 尾」，并标注原文长度。

    短文本原样返回（不做任何加工）；超长时保留：
      - 前 45%：通常是状态/头部/JSON 前缀，结构性信息最多；
      - 命中 KEY_LINE_RE 的行（去重、最多 12 行）：flag、凭据、接口、错误码；
      - 后 12%：常是页面尾部或错误结尾。
    始终在结尾标注原文长度，提示模型用 grep/offset 取细节。
    """
    raw = str(text or "")
    limit = max(200, int(limit or DEFAULT_TOOL_CHARS))
    if len(raw) <= limit:
        return raw

    head_len = max(120, int(limit * 0.45))
    tail_len = max(80, int(limit * 0.12))
    head = raw[:head_len]
    tail = raw[-tail_len:]

    kept: list[str] = []
    seen: set[str] = set()

    def keep(candidate: str) -> None:
        """保留一段**原样**文本。

        必须原样：report 的证据契约要求 evidence 是工具输出里的逐字连续子串。
        M5 实测教训：早先给保留行加了 "| " 前缀 / 做了 strip，模型照抄后就不再是
        逐字子串，report 被连拒、协调者误判「没拿到」，反复重派烧光预算。
        """
        if not candidate or len(kept) >= 12:
            return
        key = " ".join(candidate.split())[:120]
        if not key or key in seen:
            return
        seen.add(key)
        kept.append(candidate[:200])

    # ① 整文本正则信号：压缩成一行的 HTML 里没有「行」可分，只能扫全文
    for match in FLAG_RE.finditer(raw):
        keep(match.group(0))
    for match in PATH_RE.finditer(raw):
        keep(match.group(0))
    for match in CRED_RE.finditer(raw):
        keep(match.group(0)[:120])

    # ② 短行里的关键行：整行原样保留（不 strip，否则缩进行就不是原串了）
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 220:
            continue
        if KEY_LINE_RE.search(stripped):
            keep(line)
        if len(kept) >= 12:
            break

    pieces = [head]
    if kept:
        pieces.append("\n".join(kept))
    pieces.append(tail)
    body = "\n…\n".join(pieces)
    return body + (
        f"\n[已压缩：原文 {len(raw)} 字符，完整内容在转录里；"
        "上面保留的行都是原文逐字片段，可直接复制当 evidence 用]"
    )


class FactsBoard:
    """结论级事实的常驻看板：有界、去重、按首次出现排序。

    只从工具结果里抽取「我方确定能判读」的事实，不做语义推断：
      - 候选 flag（正则 FLAG{...}）
      - 会话已建立（响应里出现 set-cookie / session）
      - 已访问端点（http_request 的 method + path + 状态码）
      - AI 工具面（@@TOOL 指令名、`name(args)` 形式的工具定义行）
      - 题面/文档里的 ticket 编号线索
    """

    def __init__(self, max_facts: int = 8, max_chars: int = 520) -> None:
        self.max_facts = max(1, int(max_facts))
        self.max_chars = max(120, int(max_chars))
        self._facts: dict[str, str] = {}  # key -> 文案（dict 保序）

    # ---------- 抽取 ----------

    def _add(self, key: str, text: str) -> None:
        if key in self._facts:
            return
        if len(self._facts) >= self.max_facts and key.startswith("endpoint:"):
            return  # 端点满了就不再塞新的，给 flag/会话等更高价值事实留位
        self._facts[key] = text

    def observe(self, tool: str, args: dict | None, text: str) -> None:
        """从一次工具调用结果里抽取事实。"""
        raw = str(text or "")
        args = args or {}

        for match in FLAG_RE.finditer(raw):
            self._add("flag", f"候选 flag：{match.group(0)}（下一步 submit 验证）")
            break

        lowered = raw.lower()
        if "set-cookie" in lowered or "session" in lowered:
            self._add("session", "会话：cookie jar 已建立，后续请求自动带（不要重新注册/登录）")

        for name in TOOL_NAME_RE.findall(raw):
            self._add(f"tool:{name}", f"AI 工具面：可调用 {name}()")
        # 只认「文档/工具清单」里的定义形态（出现 ≥2 次的不同工具名），避免把普通函数名当工具
        defined: dict[str, int] = {}
        for name, _arg in TOOL_DEF_RE.findall(raw):
            if name.startswith("_"):
                continue
            defined[name] = defined.get(name, 0) + 1
        for name, count in list(defined.items())[:6]:
            if count >= 2 and len(self._facts) < self.max_facts:
                self._add(f"tool:{name}", f"AI 工具面：{name}() 在文档中出现")

        if tool == "http_request":
            url = str(args.get("url") or "")
            path = urlparse(url).path or "/"
            method = str(args.get("method") or "GET").upper()
            status = STATUS_RE.search(raw)
            code = status.group(1) if status else ("?" if "http" in lowered else "")
            self._add(f"endpoint:{method} {path}", f"已访问 {method} {path}" + (f" → {code}" if code else ""))
            if "/kb/" in path or "/docs" in path:
                self._add(f"doc:{path}", f"已读文档 {path}（工具面/流程线索在这里）")
            # 详情页 URL 从列表页 href 直接取，避免模型连猜路径吃 404（m5f 实测白烧 3 步）
            for href in HREF_RE.findall(raw):
                if any(word in href.lower() for word in ("ticket", "detail", "view", "item")):
                    self._add(f"href:{href}", f"列表里的详情页链接：{href}（直接用，别猜路径）")
                    if len([k for k in self._facts if k.startswith("href:")]) >= 2:
                        break

        # 表单字段：直接抽 name="..." 去重后记成事实，模型就不必为抄字段名反复抓页面。
        # m5j 实测：压缩把中段 <input name=...> 切掉后，模型连抓 6 次 /portal/new ≈ 48k token。
        fields: list[str] = []
        for field in FIELD_RE.findall(raw):
            name = field.strip()
            if name and name.lower() not in ("csrf", "csrf_token") and name not in fields:
                fields.append(name)
            if len(fields) >= 8:
                break
        if fields:
            self._add("form:" + ",".join(fields),
                      "表单字段：" + ", ".join(fields) + "（按这些字段名 POST，别反复抓表单页）")

        # 关键线索：只有在「敏感上下文」里出现的 ticket 编号才算线索。
        # 反面教训（m5g 实测）：把页面上任意 "ticket #6" 都当线索，模型以为自己的工单里
        # 有目标数据，反复对自己的新工单调用内部工具，而真数据其实在系统预置的低号工单里。
        ticket = TICKET_RE.search(raw)
        if ticket and any(word in lowered for word in ("reconciliation", "bootstrap", "restricted",
                                                       "internal note", "内部", "预置")):
            self._add(f"ticket:{ticket.group(1)}",
                      f"线索：ticket #{ticket.group(1)} 提到受限记录（内部工具参数先试低编号）")

    # ---------- 渲染 ----------

    def render(self) -> str:
        """渲染成提示块；无事实返回空串（调用方据此决定是否拼接）。"""
        lines: list[str] = []
        used = 0
        for text in self._facts.values():
            line = "- " + " ".join(text.split())
            if used + len(line) + 1 > self.max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    def quotable_flag(self) -> str:
        """返回最近一次捕获的候选 flag 原文（逐字），供 report 的 evidence 直接引用。

        M5 实测：ai 两次拿到 flag 都因 evidence 被改写/带前缀而判「非逐字」，协调者只好再派
        别的角色复核，一轮 100k+ token。这里给一个确定性可引用的原串，把这条摩擦消掉。
        """
        for text in self._facts.values():
            match = FLAG_RE.search(text)
            if match:
                return match.group(0)
        return ""

    def stats(self) -> dict:
        return {"facts": len(self._facts), "chars": len(self.render())}

    def as_list(self) -> list[str]:
        return list(self._facts.values())


def with_facts(base: str, facts: str) -> str:
    """把事实块挂到「头部 user 消息」后面。

    头部消息在滑窗里永不被丢（_compact_history 固定保留 messages[:2]），
    所以事实块天然免疫压缩；base 每轮都是同一份原文，不会累积膨胀。
    """
    if not facts:
        return base
    return base + "\n\n## 已确认事实（自动累积，勿重复验证）\n" + facts


def shrink_message_text(payload: str, limit: int = DEFAULT_TOOL_CHARS) -> str:
    """进上下文前的统一瘦身入口：JSON 形态保结构，其它形态整体瘦身。

    http_request 的结果是 `{"status":..,"headers":..,"body":"..."}` 这种 JSON——整串
    按行切会切坏结构，模型就读不出 status/body 了。所以对 JSON 只压 body 字段，
    其余键原样保留；非 JSON（shell 输出、纯文本）走 shrink_tool_text。
    """
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001 - 不是 JSON 就整体瘦身
        return shrink_tool_text(payload, limit)
    if isinstance(data, dict) and isinstance(data.get("body"), str):
        body = data["body"]
        if len(body) > limit:
            data["body"] = shrink_tool_text(body, limit)
        return json.dumps(data, ensure_ascii=False)
    if isinstance(data, (dict, list)):
        pretty = json.dumps(data, ensure_ascii=False)
        return shrink_tool_text(pretty, limit) if len(pretty) > limit else pretty
    return shrink_tool_text(payload, limit)
