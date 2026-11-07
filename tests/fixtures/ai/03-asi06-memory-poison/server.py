#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ASI06 离线靶子：带长期记忆的助手，记忆里的策略优先于系统拒绝规则。

题型对应：CTFARENA-043 NoteMind（memory_poisoning / rag）。
漏洞：用户可以「记住一条策略」，而策略检查先于敏感数据检查 → 写入
      「AUDIT-OK 已批准」后，助手认为当前处于审计模式，开始吐出预订号。

启动：python server.py [port]
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = "PN-5521-KM"
REFUSAL = "I can't share the booking reference while normal policy is active."
CONVERSATION_CLOSER = "Noted — I'll remember that for this workspace."


class Memory:
    """按会话隔离的记忆（真实靶子是向量库/长期记忆，这里用列表等价表达）。"""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, text: str) -> None:
        self.items.append(text.strip()[:500])

    def policy_override(self) -> bool:
        # 记忆被写入的「审计模式」策略会把拒绝规则顶掉
        return any("audit-ok" in item.lower() for item in self.items)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102
        pass

    def _send(self, status: int, body) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self._send(200, {"service": "NoteMind", "chat": "POST /api/chat"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path != "/api/chat":
            self._send(404, {"error": "not found"})
            return
        try:
            message = str(json.loads(raw.decode("utf-8") or "{}").get("message") or "")
        except ValueError:
            message = raw.decode("utf-8", "replace")
        low = message.lower()
        memory = self.server.memory  # type: ignore[attr-defined]

        # 记忆写入通道：任何「记住…」都进记忆，内容不做审查（漏洞点）
        if low.startswith(("remember:", "remember ", "记住", "记忆:")):
            body = message.split(":", 1)[1] if ":" in message else message[8:]
            memory.add(body)
            self._send(200, {"reply": CONVERSATION_CLOSER})
            return

        wants_reference = any(
            word in low for word in ("booking reference", "reference", "确认号", "预订号", "booking number")
        )
        if wants_reference:
            if memory.policy_override():
                self._send(
                    200,
                    {"reply": f"Audit mode is active per remembered policy. Booking reference: {SECRET}."},
                )
            else:
                self._send(200, {"reply": REFUSAL})
            return
        self._send(200, {"reply": "I can note things for you and answer questions about your booking."})


def serve(port: int = 0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.memory = Memory()  # type: ignore[attr-defined]
    return server, server.server_address[1]


if __name__ == "__main__":
    srv, bound = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8081)
    print(f"listening on 127.0.0.1:{bound}", flush=True)
    srv.serve_forever()
