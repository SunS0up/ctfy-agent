#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脚本化 LLM：不联网、不花钱，用来跑通整个 agent 循环。

用法见 tests/smoke.py 与 `python -m ctfy_agent demo`。
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

from . import fake_mcp_server as fake


def tool_call(name: str, arguments: dict, call_id: str | None = None) -> dict:
    return {
        "id": call_id or f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class ScriptedLLM:
    """按脚本返回 assistant 消息；脚本用完后返回一条空内容（表示收工）。"""

    def __init__(self, script: list[dict], model: str = "deepseek-v4.1-flash"):
        self.script = list(script)
        self.model = model
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.calls = 0
        self.received: list[list[dict]] = []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        self.received.append([dict(message) for message in messages])
        step = self.script.pop(0) if self.script else {"content": "", "tool_calls": []}
        return {
            "content": step.get("content") or "",
            "tool_calls": step.get("tool_calls") or [],
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "raw": {},
        }


def start_fake_mcp(mode: str = "json", expect_token: str | None = None, accept_flag: str | None = None):
    """在后台线程里起一个假 MCP 服务，返回 (server, url)。"""

    class QuietHandler(fake.Handler):
        pass

    QuietHandler.mode = mode
    QuietHandler.expect_token = expect_token
    QuietHandler.accept_flag = accept_flag or fake.ACCEPT_FLAG
    QuietHandler.log_message = lambda self, fmt, *args: None

    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1/mcp/"


def demo_script() -> list[dict]:
    """一个「记录 → 提交 → 收工」的最小脚本，只用来验证循环与幂等。"""
    return [
        {"content": "先记录题面要点。", "tool_calls": [tool_call("note", {"text": "目标：登录绕过，重点看 /login"})]},
        {
            "content": "提交候选 flag。",
            "tool_calls": [tool_call("submit_flag", {"challenge_id": "web-01", "flag": fake.ACCEPT_FLAG})],
        },
        {"content": "已提交，收工。", "tool_calls": []},
    ]
