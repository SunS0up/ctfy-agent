#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CTFy 平台封装。

平台通过 MCP 暴露 ~97 个工具（实测 ctfy 3.4.6）。这里做三件事：

  1. **语义动作 → 真实工具名**：先查 DEFAULT_TOOLS（线上实测名），不存在再按词义猜，
     并允许 config/agent.json 的 tool_map 强制覆盖。这样线上和离线假服务都能跑。
  2. **只把「解题需要的工具」交给模型**：97 个工具里有覆盖报名档案、上传题目、
     改 AWD 网络等危险动作，一律不暴露（见 MODEL_TOOLS）。提示类工具
     （list_hints / view_hint）2026-11 起放行：正赛有 hints 的题不给模型就结构性拿不到；
     view_hint **扣费且按队永久计费不可回滚**，代码里绝不自动调用，只由模型显式发起。
  3. **按 inputSchema 自适应参数形态**：真实平台是「实例中心」的
     （submit_answer/verify_answer 要 instance_id + answer），
     假服务是「题目中心」的（submit_flag 要 challenge_id + flag）。不写死，读 schema。

关键实测结论（2026-10）：
  - POST https://ctfy.tsinghua.edu.cn/api/v1/mcp/   Streamable HTTP
  - start_instance(challenge_id[, competition_id]) -> 返回实例 id(UUID) + host/port/凭据
  - verify_answer(instance_id, answer[, question_id])  → 判题但**不写提交记录**（可白试）
  - submit_answer(instance_id, answer[, question_id])  → 真正提交（消耗次数）
  - stop_instance/renew_instance/reset_instance 都以 **instance_id** 为键
"""

from __future__ import annotations

import json
from json import JSONDecodeError

from .mcp_client import McpHttpClient, mcp_tool_to_openai, render_call_result, split_tool_name

# ---------- 语义动作 → 线上真实工具名 ----------

DEFAULT_TOOLS = {
    "whoami": "get_me",
    "list_competitions": "list_competitions",
    "get_competition": "get_competition",
    "list_challenges": "list_competition_challenges",
    "get_challenge": "get_challenge",
    "start_instance": "start_instance",
    "get_instance": "get_instance",
    "stop_instance": "stop_instance",
    "renew_instance": "renew_instance",
    "verify_answer": "verify_answer",
    "submit_flag": "submit_answer",
    "submit_qa": "submit_qa_answer",
    "leaderboard": "get_scoreboard",
    "list_instances": "list_instances",
    "my_solves": "get_my_solves",
    "my_progress": "get_my_milestone_progress",
    "list_submissions": "list_submissions",
}

# 允许暴露给模型的工具白名单（其余 97 个里的一律不给模型）
MODEL_TOOLS = frozenset({
    # 只读：侦察
    "get_me",
    "list_competitions",
    "get_competition",
    "list_competition_challenges",
    "get_challenge",
    "list_challenge_attachments",
    "download_challenge_attachment",
    "list_instances",
    "get_instance",
    "get_instance_status",
    "get_instance_traffic",
    "list_instance_attachments",
    "download_instance_attachment",
    "get_my_solves",
    "get_my_milestone_progress",
    "list_submissions",
    "get_scoreboard",
    "search_challenges",
    # 只读：提示（A0 附带项）。list_hints 免费列层级，view_hint 扣费且不可回滚
    "list_hints",
    "view_hint",
    # 写：由 solver 的幂等/频控/上限约束
    "start_instance",
    "stop_instance",
    "renew_instance",
    "verify_answer",
    "submit_answer",
    "submit_qa_answer",
})

# 自动归类用的词表（DEFAULT_TOOLS 里没有该工具时的回退）
KIND_WORDS = {
    "list_challenges": {"list", "challenges", "challenge", "problems", "browse", "catalog"},
    "get_challenge": {"get", "detail", "details", "info", "view", "challenge"},
    "start_instance": {"start", "launch", "create", "instance", "env", "environment", "up", "deploy"},
    "stop_instance": {"stop", "destroy", "kill", "delete", "instance", "env", "down"},
    "submit_flag": {"submit", "flag", "answer", "check", "verify"},
    "leaderboard": {"leaderboard", "rank", "ranking", "scoreboard", "board", "scores"},
}

KIND_EXPECT = {
    "list_challenges": "read",
    "get_challenge": "read",
    "leaderboard": "read",
    "start_instance": "write",
    "stop_instance": "write",
    "submit_flag": "write",
}


class PlatformError(RuntimeError):
    pass


class CtfyPlatform:
    def __init__(self, mcp_url: str, token: str, *, timeout: float = 30.0, tool_map: dict | None = None,
                 verbose: bool = False):
        self.client = McpHttpClient(mcp_url, token, timeout=timeout, verbose=verbose)
        self.tool_map = dict(tool_map or {})
        self.tools: list[dict] = []
        self._by_name: dict[str, dict] = {}
        self._resolved: dict[str, str] = {}

    # ---------- 连接 ----------

    def connect(self) -> dict:
        result = self.client.initialize()
        self.tools = self.client.list_tools()
        self._by_name = {tool.get("name") or "": tool for tool in self.tools}
        self._resolved.clear()
        return result or {}

    @property
    def server_info(self) -> dict:
        return self.client.server_info or {}

    def tool_names(self) -> list[str]:
        return [tool.get("name") or "?" for tool in self.tools]

    def schema_of(self, name: str) -> dict:
        tool = self._by_name.get(name) or {}
        return tool.get("inputSchema") or {}

    def prop_names(self, name: str) -> set[str]:
        return set((self.schema_of(name).get("properties") or {}).keys())

    # ---------- 暴露给模型的工具（白名单） ----------

    def _allowlist_active(self) -> bool:
        """只有确认是真实平台时才收紧工具面（假服务/自测全放开）。"""
        names = set(self._by_name)
        return "get_me" in names or "submit_answer" in names

    def model_tools(self) -> list[dict]:
        """只返回白名单内的工具；非真实平台（如离线假服务）退回全部。"""
        if not self._allowlist_active():
            return list(self.tools)
        allowed = [tool for tool in self.tools if (tool.get("name") or "") in MODEL_TOOLS]
        return allowed or list(self.tools)

    def openai_tools(self) -> list[dict]:
        return [mcp_tool_to_openai(tool) for tool in self.model_tools()]

    def openai_tools_for(self, names: set[str]) -> list[dict]:
        """只把 names 指定的平台工具转成 OpenAI 规格（多智能体按角色配工具面）。"""
        return [mcp_tool_to_openai(tool) for tool in self.tools if (tool.get("name") or "") in names]

    def is_model_tool(self, name: str) -> bool:
        names = {tool.get("name") or "" for tool in self.model_tools()}
        return name in names

    # ---------- 语义动作解析 ----------

    def resolve(self, kind: str) -> str:
        """config.tool_map > 线上实测名 > 自动归类。只返回平台上真实存在的工具名。"""
        if kind in self._resolved:
            return self._resolved[kind]

        candidate = self.tool_map.get(kind) or DEFAULT_TOOLS.get(kind)
        if candidate and candidate in self._by_name:
            self._resolved[kind] = candidate
            return candidate
        if self.tool_map.get(kind):
            # 显式指定了但平台上没有：直接报错，别悄悄换成别的工具
            raise PlatformError(
                f"tool_map 把「{kind}」指定为 {self.tool_map[kind]}，但平台没有这个工具。\n"
                f"现有工具：{', '.join(sorted(self._by_name))}"
            )

        words = KIND_WORDS.get(kind)
        if not words:
            raise PlatformError(f"未知语义动作 {kind}，也没有对应默认工具")
        expect = KIND_EXPECT.get(kind)
        best_name, best_score = None, 0
        for name in self._by_name:
            tokens = set(split_tool_name(name))
            score = len(tokens & words)
            if score == 0:
                continue
            if kind == "submit_flag":
                # 关键：verify_* 是不记账的探针，绝不能当提交用
                if name.startswith("verify"):
                    continue
                if not (tokens & {"submit", "answer", "flag"}):
                    continue
            if kind == "start_instance" and (tokens & {"stop", "down", "delete", "destroy"}):
                continue
            if kind == "stop_instance" and not (tokens & {"stop", "down", "delete", "destroy", "kill"}):
                continue
            if expect and _classify(name) == expect:
                score += 0.5
            if score > best_score:
                best_name, best_score = name, score

        if not best_name:
            raise PlatformError(
                f"没能在平台工具里找到「{kind}」对应的工具。\n"
                f"现有工具：{', '.join(sorted(self._by_name)) or '(空)'}\n"
                f'可在 config/agent.json 的 tool_map 里手动指定，例如 {{"tool_map": {{"{kind}": "真实工具名"}}}}'
            )
        self._resolved[kind] = best_name
        return best_name

    def try_resolve(self, kind: str) -> str | None:
        try:
            return self.resolve(kind)
        except PlatformError:
            return None

    def resolution_report(self) -> dict:
        report = {}
        for kind in set(KIND_WORDS) | set(DEFAULT_TOOLS):
            report[kind] = self.try_resolve(kind)
        return report

    # ---------- 参数校验 ----------

    def _clean_args(self, name: str, arguments: dict | None) -> dict:
        """轻量校验：required 缺失报错，integer 自动纠正类型。"""
        cleaned = dict(arguments or {})
        schema = self.schema_of(name)
        if not schema:
            return cleaned
        properties = schema.get("properties") or {}
        for field in schema.get("required") or []:
            if cleaned.get(field) in (None, ""):
                raise PlatformError(f"{name}: 缺少必填参数 {field}")
        for field, value in list(cleaned.items()):
            prop = properties.get(field) or {}
            if prop.get("type") == "integer" and not isinstance(value, int):
                try:
                    cleaned[field] = int(value)
                except (TypeError, ValueError):
                    raise PlatformError(f"{name}: 参数 {field} 应为整数，收到 {value!r}")
        return cleaned

    # ---------- 调用 ----------

    def call(self, name: str, arguments: dict | None = None, *, retries: int = 0):
        arguments = self._clean_args(name, arguments)
        return self.client.call_tool(name, arguments, retries=retries)

    def call_raw(self, name: str, arguments: dict | None = None, *, retries: int = 0):
        """返回原始 result dict（含 isError），供提交判定精确读取。"""
        return self.call(name, arguments, retries=retries)

    def call_text(self, name: str, arguments: dict | None = None, *, retries: int = 0) -> str:
        result = self.call(name, arguments, retries=retries)
        text = render_call_result(result)
        if isinstance(result, dict) and result.get("isError"):
            text = f"[工具报错] {text}"
        return text

    @staticmethod
    def _as_json(text: str):
        """解析工具返回里的第一个 JSON 值。

        平台返回常是「content 文本 + structuredContent 美化 JSON」两段拼接，
        所以用 raw_decode 只取开头那个 JSON，忽略后面的重复美化段。
        """
        raw = text or ""
        if raw.startswith("-----"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else ""
        raw = raw.removeprefix("[工具报错] ").lstrip()
        if not raw:
            return None
        try:
            value, _ = json.JSONDecoder().raw_decode(raw)
            return value
        except JSONDecodeError:
            return None

    # ---------- 便捷方法 ----------

    def whoami(self):
        return self._json_or_text("whoami", {})

    def list_competitions(self, **filters):
        return self._json_or_text("list_competitions", {k: v for k, v in filters.items() if v is not None})

    def get_competition(self, competition_id: str):
        return self._json_or_text("get_competition", {"competition_id": competition_id})

    def list_challenges(self, competition_id: str | None = None, **filters):
        payload = {k: v for k, v in filters.items() if v is not None}
        if competition_id:
            payload["competition_id"] = competition_id
        return self._json_or_text("list_challenges", payload)

    def get_challenge(self, challenge_id: str, competition_id: str | None = None):
        payload = {"challenge_id": challenge_id}
        if competition_id:
            payload["competition_id"] = competition_id
        return self._json_or_text("get_challenge", payload)

    def start_instance(self, challenge_id: str, competition_id: str | None = None):
        payload = {"challenge_id": challenge_id}
        if competition_id:
            payload["competition_id"] = competition_id
        return self._json_or_text("start_instance", payload)

    def get_instance(self, instance_id: str):
        return self._json_or_text("get_instance", {"instance_id": instance_id})

    def stop_instance(self, instance_id: str):
        return self._json_or_text("stop_instance", {"instance_id": instance_id})

    def list_instances(self):
        return self._json_or_text("list_instances", {})

    def my_solves(self):
        return self._json_or_text("my_solves", {})

    def leaderboard(self, competition_id: str | None = None, **filters):
        payload = {k: v for k, v in filters.items() if v is not None}
        if competition_id:
            payload["competition_id"] = competition_id
        return self._json_or_text("leaderboard", payload)

    def _json_or_text(self, kind: str, payload: dict):
        name = self.resolve(kind)
        text = self.call_text(name, payload, retries=2 if _classify(name) == "read" else 0)
        parsed = self._as_json(text)
        return parsed if parsed is not None else text


def _classify(name: str) -> str:
    return "read" if (set(split_tool_name(name)) & {
        "list", "get", "search", "read", "view", "info", "status", "query", "fetch",
        "my", "show", "describe", "board", "detail", "details", "download", "summary", "traffic",
    }) and not (set(split_tool_name(name)) & {
        "submit", "answer", "start", "stop", "launch", "create", "delete", "remove",
        "run", "exec", "reset", "restart", "kill", "update", "write", "set", "put", "post",
        "upload", "deploy", "destroy", "renew", "verify", "join", "leave", "save", "reply",
    }) else "write"
