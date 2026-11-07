#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek V4.1 Flash 客户端（OpenAI 兼容 /chat/completions，纯标准库）。

只做一件事：把 messages + tools 发出去，拿回 assistant 消息（可能带 tool_calls）。
重试策略：429/5xx/网络错误可退避重试——聊天补全没有副作用，重试是安全的。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from json import JSONDecodeError

from .httputil import SameOriginRedirect, build_opener, read_limited


class LLMError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class DeepSeekLLM:
    """DeepSeek V4.1 Flash（也可指向任何 OpenAI 兼容端点）。"""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-v4.1-flash",
        base_url: str = "https://api.deepseek.com/v1",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = 120.0,
        debug: bool = False,
        budget=None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.debug = debug
        # 费用刹车：budget 有余额才允许发下一次请求（见 budget.py）
        self.budget = budget
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.calls = 0
        # 同源重定向护栏：跨源重定向直接报错，防止 API Key 外送
        self._opener = build_opener(SameOriginRedirect(self.base_url))

    # ---------- 传输 ----------

    def _post(self, path: str, payload: dict, retries: int = 3):
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        attempt = 0
        while True:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return json.loads(read_limited(response).decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = read_limited(exc, 600).decode("utf-8", "replace")
                except Exception:
                    pass
                hint = ""
                if exc.code == 401:
                    hint = "（API Key 无效）"
                elif exc.code == 402:
                    hint = "（余额不足）"
                elif exc.code == 429:
                    hint = "（限速或额度用尽）"
                error = LLMError(
                    f"HTTP {exc.code} {exc.reason}{hint}: {detail[:200]}",
                    status=exc.code,
                    retryable=exc.code in (429, 500, 502, 503, 504),
                )
                if not (error.retryable and attempt < retries):
                    raise error from None
            except urllib.error.URLError as exc:
                error = LLMError(f"无法连接 {url}: {exc.reason}", retryable=True)
                if attempt >= retries:
                    raise error from None
            except JSONDecodeError as exc:
                raise LLMError(f"响应不是合法 JSON: {exc}") from None

            attempt += 1
            delay = min(2 ** attempt, 20)
            time.sleep(delay)

    # ---------- 接口 ----------

    def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice: str = "auto") -> dict:
        # 硬刹车：预算耗尽就直接拒绝发请求，不会产生费用
        if self.budget is not None:
            self.budget.check()

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        data = self._post("/chat/completions", payload)
        self.calls += 1
        usage = data.get("usage") or {}
        for key in self.usage:
            self.usage[key] += int(usage.get(key) or 0)
        if self.budget is not None:
            self.budget.record(usage)

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"响应里没有 choices: {json.dumps(data, ensure_ascii=False)[:400]}")
        message = choices[0].get("message") or {}
        return {
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
            "finish_reason": choices[0].get("finish_reason"),
            "usage": usage,
            "raw": message,
        }

    def ping(self) -> dict:
        """探活：列模型列表，用于 doctor。"""
        request = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=min(self.timeout, 30)) as response:
                data = json.loads(read_limited(response, 1 << 20).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMError(f"HTTP {exc.code} {exc.reason}（Key 或端点可能不对）", status=exc.code) from None
        except urllib.error.URLError as exc:
            raise LLMError(f"无法连接 {self.base_url}: {exc.reason}", retryable=True) from None
        models = [item.get("id") for item in (data.get("data") or []) if isinstance(item, dict)]
        return {"models": models, "configured": self.model}
