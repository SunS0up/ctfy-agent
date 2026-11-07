#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 客户端（Streamable HTTP，Python 3.9+ 纯标准库）。

负责与 CTFy 平台的 MCP 端点通信：
  1. initialize / notifications/initialized 握手
  2. tools/list 拉取平台工具清单与参数 schema
  3. tools/call 调用工具

安全约定（写死在实现里，不靠调用方自觉）：
  - 写操作（submit/start/stop/...）不自动重试，避免重复提交 flag；
  - 只有只读调用才允许指数退避重试；
  - 令牌只保存在内存里，不打印全文、不落盘。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from json import JSONDecodeError

from .httputil import MAX_READ_BYTES, SameOriginRedirect, build_opener, read_limited

DEFAULT_MCP_URL = "https://ctfy.tsinghua.edu.cn/api/v1/mcp/"
DEFAULT_PROTOCOL = "2025-06-18"
CLIENT_INFO = {"name": "ctfy-agent", "version": "0.2.0"}


# 只读 / 写操作的关键词判据（按词切分匹配，避免 "search_writeups" 被当成写操作）
READONLY_WORDS = frozenset({
    "list", "get", "search", "read", "view", "info", "status", "query", "fetch",
    "my", "show", "describe", "leaderboard", "detail", "details", "download", "summary",
})
MUTATING_WORDS = frozenset({
    "submit", "answer", "flag", "start", "stop", "launch", "create", "delete", "remove",
    "run", "exec", "reset", "restart", "kill", "update", "write", "set", "put", "post",
    "patch", "upload", "deploy", "destroy", "cancel", "close", "open",
})


def split_tool_name(name: str) -> list[str]:
    """把工具名切成小写词：snake_case / kebab-case / camelCase 都支持。"""
    buffered = []
    for index, char in enumerate(name or ""):
        if char.isupper() and index > 0 and not (name[index - 1].isupper()):
            buffered.append("_")
        buffered.append(char if char.isalnum() else "_")
    return [token for token in "".join(buffered).lower().split("_") if token]



class McpError(RuntimeError):
    """带重试语义的错误。retryable 只对只读幂等调用有意义。"""

    def __init__(self, message: str, *, retryable: bool = False, http_status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status


def mask_token(token: str) -> str:
    if not token:
        return "(空)"
    # 只暴露前 4 个字符用于识别（pf_ / sk- 前缀），不再露尾部
    return token[:4] + "***" if len(token) > 4 else "***"


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def force_utf8_when_piped() -> None:
    """被管道/重定向时按 UTF-8 输出，避免 Windows 控制台代码页导致乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):
            continue
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 编码不可改时保持原样
            pass


def classify_tool(name: str) -> str:
    """返回 read / write / unknown。unknown 按写处理（宁严勿松）。"""
    tokens = set(split_tool_name(name))
    if tokens & MUTATING_WORDS:
        return "write"
    if tokens & READONLY_WORDS:
        return "read"
    return "unknown"


class McpHttpClient:
    """MCP Streamable HTTP 客户端。"""

    def __init__(self, url: str, token: str, timeout: float = 30.0, verbose: bool = False):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.verbose = verbose
        self.session_id: str | None = None
        self.protocol_version = DEFAULT_PROTOCOL
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions: str | None = None
        self._msg_id = 0
        # 同源重定向护栏：跨源重定向直接报错，防止 Authorization 头外送
        self._opener = build_opener(SameOriginRedirect(url))

    # ---------- 底层传输 ----------

    def _debug(self, text: str) -> None:
        if self.verbose:
            print(f"[debug] {text}", file=sys.stderr)

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
            "MCP-Protocol-Version": self.protocol_version,
            "User-Agent": "ctfy-probe/0.1 (stdlib-urllib)",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _post(self, payload: dict, expect_response: bool = True):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers=self._headers(), method="POST")
        self._debug(f"→ {payload.get('method')} id={payload.get('id')} session={self.session_id}")

        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = read_limited(exc, 1000).decode("utf-8", "replace").strip()
            except Exception:  # pragma: no cover - 读错误体失败时忽略
                pass
            hint = ""
            if exc.code in (401, 403):
                hint = "（令牌无效 / 已过期 / 权限不足：到 设置 → API 令牌 重新签发）"
            elif exc.code == 429:
                hint = "（被限速：降低调用频率，别轮询）"
            raise McpError(
                f"HTTP {exc.code} {exc.reason}{hint}: {detail[:200]}",
                retryable=exc.code in (429, 500, 502, 503, 504),
                http_status=exc.code,
            ) from None
        except urllib.error.URLError as exc:
            raise McpError(f"无法连接 {self.url}: {exc.reason}", retryable=True) from None
        except (TimeoutError, socket.timeout):
            # 读超时不是 URLError（实测从 getresponse() 直接抛 TimeoutError）：
            # 不兜住会让整条 agent 崩掉——容器里等于不写 result.json = 0 分。
            raise McpError(f"MCP 响应超时（{self.timeout:.0f}s）：{self.url}", retryable=True) from None

        new_session = response.headers.get("Mcp-Session-Id")
        if new_session:
            self.session_id = new_session
        content_type = (response.headers.get("Content-Type") or "").lower()
        self._debug(f"← {response.status} content-type={content_type or '(none)'}")

        try:
            if not expect_response:
                read_limited(response, 1 << 20)  # 202 Accepted 通常为空体，读走并限制体积
                return None
            if "text/event-stream" in content_type:
                return self._read_sse(response, payload.get("id"))
            raw = read_limited(response)
            if not raw.strip():
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except JSONDecodeError:
                raise McpError(
                    f"响应不是 JSON（Content-Type={content_type or 'none'}）：{raw[:200]!r}"
                ) from None
        except (TimeoutError, socket.timeout):
            raise McpError(f"响应体读取超时（{self.timeout:.0f}s）：{self.url}", retryable=True) from None
        finally:
            response.close()

    def _read_sse(self, response, want_id):
        data_lines: list[str] = []
        total = 0
        try:
            for raw_line in response:
                total += len(raw_line)
                if total > MAX_READ_BYTES:
                    raise McpError("SSE 响应体过大（超过 8 MiB）")
                line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if not data_lines:
                        continue
                    message = self._decode_event(data_lines)
                    data_lines = []
                    if message is None:
                        continue
                    if want_id is not None and message.get("id") != want_id:
                        self._debug(f"忽略非目标消息：{message.get('method') or message.get('id')}")
                        continue
                    return message
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
        except (socket.timeout, TimeoutError):
            raise McpError("SSE 读取超时", retryable=True) from None

        if data_lines and want_id is None:
            return self._decode_event(data_lines)
        return None

    @staticmethod
    def _decode_event(data_lines: list[str]):
        text = "\n".join(data_lines).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except JSONDecodeError:
            return None

    # ---------- JSON-RPC ----------

    def request(self, method: str, params=None, *, retries: int = 0, idempotent: bool = False):
        self._msg_id += 1
        request_id = self._msg_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params

        attempt = 0
        while True:
            try:
                message = self._post(payload, expect_response=True)
                break
            except McpError as exc:
                if not (exc.retryable and idempotent and attempt < retries):
                    raise
                attempt += 1
                delay = min(2 ** attempt, 10)
                print(f"[warn] {method} 失败（{exc}），{delay}s 后重试 {attempt}/{retries}", file=sys.stderr)
                time.sleep(delay)

        if message is None:
            raise McpError(f"{method}: 服务端返回空响应体（期望 JSON-RPC 响应）")
        if not isinstance(message, dict):
            raise McpError(f"{method}: 响应格式异常：{message!r}")
        if "error" in message:
            error = message["error"] or {}
            raise McpError(f"{method}: JSON-RPC 错误 {error.get('code')}: {error.get('message')}")
        return message.get("result")

    def notify(self, method: str, params=None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._post(payload, expect_response=False)
        except McpError as exc:
            # 通知失败不致命：多数服务端对 initialized 只回 202
            print(f"[warn] 通知 {method} 发送失败：{exc}", file=sys.stderr)

    # ---------- MCP 方法 ----------

    def initialize(self):
        result = self.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            retries=2,
            idempotent=True,
        )
        if isinstance(result, dict):
            self.server_info = result.get("serverInfo") or {}
            version = result.get("protocolVersion")
            if version:
                self.protocol_version = version
            self.capabilities = result.get("capabilities") or {}
            self.instructions = result.get("instructions")
        self.notify("notifications/initialized")
        return result

    def list_tools(self):
        tools: list[dict] = []
        cursor = None
        for _ in range(20):
            params = {} if cursor is None else {"cursor": cursor}
            result = self.request("tools/list", params, retries=2, idempotent=True) or {}
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict | None = None, *, retries: int = 0):
        # 写操作不可重试，所以 idempotent 只在 retries > 0 时成立（由调用方保证只读）
        return self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            retries=retries,
            idempotent=retries > 0,
        )


# ---------- 结果渲染 ----------


def render_call_result(result) -> str:
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, indent=2)
    pieces = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            pieces.append(item.get("text") or "")
        else:
            pieces.append(json.dumps(item, ensure_ascii=False))
    if result.get("structuredContent") is not None:
        pieces.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=2))
    if not pieces:
        pieces.append(json.dumps(result, ensure_ascii=False, indent=2))
    flag = " (isError=true)" if result.get("isError") else ""
    return f"----- 工具返回{flag} -----\n" + "\n".join(pieces)


def mcp_tool_to_openai(tool: dict) -> dict:
    """把 MCP 的 tool 描述转成 OpenAI/DeepSeek function calling 的 tool 规格。"""
    schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
    if "type" not in schema:
        schema = {"type": "object", "properties": schema.get("properties") or {}}
    return {
        "type": "function",
        "function": {
            "name": tool.get("name") or "unknown_tool",
            "description": (tool.get("description") or "").strip()[:1024],
            "parameters": schema,
        },
    }

