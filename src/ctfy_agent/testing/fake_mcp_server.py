#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""包内假 MCP 服务：离线自测与 demo 演练，不联网、不碰比赛平台、不存真实数据。

行为贴近线上：Bearer 鉴权、initialize 下发 Mcp-Session-Id、后续请求校验会话、
tools/list 返回假工具、tools/call 返回 content[].text。

安全收紧（相对 tests 版）：
  - 请求体上限 1 MiB，超限回 413（防止本机 DoS）；
  - 默认只绑 127.0.0.1，绑到非回环地址时告警。
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION_ID = "sess-fake-0001"
PROTOCOL_VERSION = "2025-06-18"
ACCEPT_FLAG = "flag{fake_ok}"
MAX_BODY_LEN = 1024 * 1024  # 1 MiB


TOOLS = [
    {
        "name": "list_challenges",
        "description": "列出赛事题目，可按关键词或分类过滤（假数据）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "category": {"type": "string", "enum": ["web", "pwn", "ai"]},
                "limit": {"type": "integer", "default": 20},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_challenge",
        "description": "读取单道题目的详细信息（假数据）",
        "inputSchema": {
            "type": "object",
            "properties": {"challenge_id": {"type": "string"}},
            "required": ["challenge_id"],
        },
    },
    {
        "name": "start_instance",
        "description": "为一道在线交互题启动独立实例（写操作，可逆）",
        "inputSchema": {
            "type": "object",
            "properties": {"challenge_id": {"type": "string"}},
            "required": ["challenge_id"],
        },
    },
    {
        "name": "stop_instance",
        "description": "停止题目实例（写操作）",
        "inputSchema": {
            "type": "object",
            "properties": {"challenge_id": {"type": "string"}},
            "required": ["challenge_id"],
        },
    },
    {
        "name": "submit_flag",
        "description": "提交答案（写操作，不可重试）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "challenge_id": {"type": "string"},
                "flag": {"type": "string"},
            },
            "required": ["challenge_id", "flag"],
        },
    },
    {
        "name": "get_leaderboard",
        "description": "读取排行榜（只读）",
        "inputSchema": {"type": "object", "properties": {"top": {"type": "integer", "default": 10}}, "required": []},
    },
]

CHALLENGES = [
    # web-01 复刻真实平台 get_challenge 的题型字段形状（tags/difficulty/solves_count）
    {"id": "web-01", "name": "Login Bypass", "category": "web", "points": 200,
     "difficulty": "easy", "solves_count": 10,
     "tags": ["authentication_failures", "weak_password", "osint"]},
    {"id": "pwn-03", "name": "Baby Heap", "category": "pwn", "points": 350, "solved": 12},
    {"id": "ai-02", "name": "Prompt Leak", "category": "ai", "points": 300, "solved": 7},
]


def text_result(text: str, is_error: bool = False) -> dict:
    payload = {"content": [{"type": "text", "text": text}]}
    if is_error:
        payload["isError"] = True
    return payload


def handle_tool(name: str, arguments: dict, accept_flag: str | None = None) -> dict:
    if name == "list_challenges":
        keyword = (arguments.get("keyword") or "").lower()
        category = arguments.get("category")
        rows = [c for c in CHALLENGES if (not category or c["category"] == category)]
        rows = [c for c in rows if not keyword or keyword in c["name"].lower() or keyword in c["id"]]
        return text_result(json.dumps(rows, ensure_ascii=False, indent=2))

    if name == "get_challenge":
        cid = arguments.get("challenge_id")
        for item in CHALLENGES:
            if item["id"] == cid:
                return text_result(json.dumps(item, ensure_ascii=False, indent=2))
        return text_result(f"challenge {cid!r} not found", is_error=True)

    if name == "start_instance":
        cid = arguments.get("challenge_id")
        return text_result(
            json.dumps({"challenge_id": cid, "host": "10.0.0.9", "port": 31001, "ttl": 7200}, ensure_ascii=False)
        )

    if name == "stop_instance":
        return text_result(json.dumps({"challenge_id": arguments.get("challenge_id"), "state": "stopped"}, ensure_ascii=False))

    if name == "submit_flag":
        flag = str(arguments.get("flag") or "")
        accepted = bool(accept_flag) and flag == accept_flag
        return text_result(
            json.dumps(
                {"accepted": accepted, "reason": "ok" if accepted else "wrong flag（假服务，不产生真实提交）"},
                ensure_ascii=False,
            )
        )

    if name == "get_leaderboard":
        top = int(arguments.get("top") or 3)
        rows = [{"rank": i + 1, "team": f"team-{i + 1}", "score": 1000 - i * 137} for i in range(top)]
        return text_result(json.dumps(rows, ensure_ascii=False, indent=2))

    return text_result(f"unknown tool {name!r}", is_error=True)


class Handler(BaseHTTPRequestHandler):
    mode = "json"
    protocol = PROTOCOL_VERSION
    expect_token = None
    accept_flag = ACCEPT_FLAG

    # ---------- 输出 ----------

    def log_message(self, fmt, *args):  # 精简访问日志
        sys.stderr.write("[fake-mcp] " + (fmt % args) + "\n")

    def _send(self, body: bytes, status: int, content_type: str, extra_headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_message(self, message: dict, status: int = 200, extra_headers: dict | None = None):
        if self.mode == "sse":
            body = ("event: message\ndata: " + json.dumps(message, ensure_ascii=False) + "\n\n").encode("utf-8")
            self._send(body, status, "text/event-stream; charset=utf-8", extra_headers)
        else:
            self._send(json.dumps(message, ensure_ascii=False).encode("utf-8"), status, "application/json", extra_headers)

    def _accepted(self):
        self._send(b"", 202, "application/json")

    def _rpc_error(self, request_id, code: int, message: str):
        self._send_message({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    # ---------- 协议 ----------

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_LEN:
            self._send(json.dumps({"error": "payload too large"}).encode(), 413, "application/json")
            return
        raw = self.rfile.read(length) if length > 0 else b""

        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer ") or len(auth) <= len("Bearer "):
            self._send(json.dumps({"error": "unauthorized"}).encode(), 401, "application/json")
            return
        if self.expect_token and auth != f"Bearer {self.expect_token}":
            self._send(json.dumps({"error": "invalid token"}).encode(), 401, "application/json")
            return

        try:
            message = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._send(json.dumps({"error": "bad json"}).encode(), 400, "application/json")
            return

        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            result = {
                "protocolVersion": self.protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake-ctfy", "version": "0.0.1"},
                "instructions": "本地假服务，仅用于自测与 demo。",
            }
            self._send_message(
                {"jsonrpc": "2.0", "id": request_id, "result": result},
                extra_headers={"Mcp-Session-Id": SESSION_ID},
            )
            return

        # 非 initialize 请求必须带正确会话（模拟有状态服务端）
        if self.headers.get("Mcp-Session-Id") != SESSION_ID:
            self._send(json.dumps({"error": "missing or invalid Mcp-Session-Id"}).encode(), 400, "application/json")
            return

        if method == "notifications/initialized":
            self._accepted()
            return

        if method == "tools/list":
            self._send_message({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
            return

        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            self._send_message({"jsonrpc": "2.0", "id": request_id, "result": handle_tool(name, arguments, self.accept_flag)})
            return

        self._rpc_error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    parser = argparse.ArgumentParser(description="包内假 MCP 服务（自测/demo 用）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--mode", choices=["json", "sse"], default="json")
    parser.add_argument("--expect-token", default=None, help="若提供，只接受该令牌，用于测试 401 分支")
    parser.add_argument("--accept-flag", default=ACCEPT_FLAG, help="submit_flag 判为通过的那个 flag")
    args = parser.parse_args()

    Handler.mode = args.mode
    Handler.expect_token = args.expect_token
    Handler.accept_flag = args.accept_flag

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[warn] 绑定到非回环地址 {args.host}，会把假服务暴露到网络，仅测试用途。", file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[fake-mcp] listening on http://{args.host}:{args.port}/v1/mcp/ mode={args.mode}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
