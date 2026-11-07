#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：会话 cookie 种在 302 上时也要保住（真实 CTFARENA-038 的登录形态）。

背景（M5 真机实测发现）：SupportMind 的 `POST /register` 返回 **302 Location:/portal/
+ Set-Cookie: session=…**。urllib 跟随跳转后只把最终响应交给调用方，而 tools_local
的自建 jar 原先只从「最终响应头」捞 cookie → 会话丢失，表现为：
  注册成功（200/302）→ 之后每个请求都回到未登录页（字节数恒等于登录页），
  模型于是反复注册/登录，两个真机 run 各烧 200k+ token 也没进展。

本用例用离线靶子钉住修复：302 上种 cookie，第二次请求必须带上它。
"""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ctfy_agent.tools_local import LocalTools  # noqa: E402

SID = "sess-abc-123"


class _Store:
    def add_note(self, *args, **kwargs):  # noqa: D102
        pass


class _Handler(BaseHTTPRequestHandler):
    """POST /login → 302 + Set-Cookie；GET /home 有 cookie 才给 welcome，否则 401。"""

    def log_message(self, *args):  # noqa: D102
        pass

    def _send(self, status: int, body: bytes, extra: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/login":
            # 关键形态：会话种在 302 上
            self._send(
                302,
                b'<html><a href="/home">Redirecting...</a></html>',
                {"Location": "/home", "Set-Cookie": f"sid={SID}; HttpOnly; Path=/; SameSite=Lax"},
            )
        else:
            self._send(404, b"not found")

    def do_GET(self):  # noqa: N802
        cookie = self.headers.get("Cookie") or ""
        if self.path == "/home":
            if SID in cookie:
                self._send(200, b"welcome, signed in")
            else:
                self._send(401, b"please sign in")
        elif self.path == "/login":
            self._send(200, b"<form>login</form>")
        else:
            self._send(404, b"not found")


def case_http_302_cookie(tmp: Path) -> None:
    main = sys.modules.get("__main__")
    reporter = getattr(main, "ok", None)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        tools = LocalTools(_Store(), f"case-302-{port}")
        tools.allow_instance("127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"

        # 1) 登录：302 上种 cookie，工具应跟随并拿到 200 的 welcome 正文
        home = tools.http_request(base + "/login", method="POST", body="u=1&p=2")
        assert "welcome" in home, f"登录跳转后没到 /home：{home[:200]}"

        # 2) 会话延续：后续请求自动带 Cookie
        again = tools.http_request(base + "/home")
        assert "welcome" in again, f"第二次请求没带会话 cookie（302 cookie 丢失）：{again[:200]}"

        # 3) 负向对照：换一个未登录的 host 不该带这个 cookie
        fresh = LocalTools(_Store(), f"case-302-fresh-{port}")
        fresh.allow_instance("127.0.0.1", port)
        denied = fresh.http_request(base + "/home")
        assert "sign in" in denied or "401" in denied, f"新会话不该有 cookie：{denied[:200]}"

        label = "HTTP 会话：302 上的 Set-Cookie 也入 jar（真机 038 登录形态回归）"
        if callable(reporter):
            reporter(label)
        else:
            print(f"PASS {label}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    import traceback

    try:
        case_http_302_cookie(ROOT / "dist" / "_http302_standalone")
        print("OK")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
