# -*- coding: utf-8 -*-
"""在本机验证「OpenAI 兼容」模型网关是否可用（浏览器打开 /v1 看到的 404 是正常的）。

用法（key 只从环境变量读，不落盘、不进 argv）：
    $env:DEEPSEEK_API_KEY = "sk-..."
    python tools/check_llm.py
    python tools/check_llm.py --model DeepSeek-V4.1-Flash-1017 --base https://ai.paratera.com/v1

检查三件事：
    1. GET  {base}/models           —— 端点存在、key 有效（401 = key 错）
    2. POST {base}/chat/completions —— chat 方言可用、模型名存在
    3. 打印回复首行              —— 端到端真的能出字

只用 stdlib，和容器里的 llm.py 走同一条路径（/chat/completions）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "https://ai.paratera.com/v1"
DEFAULT_MODEL = "DeepSeek-V4.1-Flash-1017"


def key() -> str:
    for name in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            print(f"[key] 使用环境变量 {name}（{value[:6]}…{value[-4:]}）")
            return value
    raise SystemExit("没有 key：先 $env:DEEPSEEK_API_KEY = \"sk-...\"")


def call(url: str, api_key: str, payload: dict | None = None, timeout: int = 60):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Authorization", f"Bearer {api_key}")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="验证 OpenAI 兼容网关")
    parser.add_argument("--base", default=os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE)
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL)
    args = parser.parse_args(argv)
    base = args.base.rstrip("/")
    api_key = key()

    print(f"[1] GET {base}/models")
    status, body = call(f"{base}/models", api_key)
    if status == 200:
        try:
            names = [item.get("id") for item in (json.loads(body).get("data") or [])]
        except json.JSONDecodeError:
            names = []
        print(f"    HTTP 200，{len(names)} 个模型")
        if names:
            print("    前几个：" + ", ".join(str(n) for n in names[:8]))
        if args.model not in names and names:
            print(f"    ⚠ 列表里没有 {args.model}，仍继续测 chat（网关可能不列全）")
    else:
        print(f"    HTTP {status}: {body[:300]}")
        print("    401 → key 错或过期；404 → base 路径不对；0 → 网络不通")

    print(f"[2] POST {base}/chat/completions  model={args.model}")
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": "只回两个字：能通"}],
        "max_tokens": 32,
    }
    status, body = call(f"{base}/chat/completions", api_key, payload)
    if status != 200:
        print(f"    HTTP {status}: {body[:500]}")
        print("    400 多半是模型名不对；401 是 key；404 是方言/路径不对（不是 OpenAI 兼容）")
        return 1
    try:
        parsed = json.loads(body)
        content = parsed["choices"][0]["message"].get("content") or ""
        usage = parsed.get("usage") or {}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        print(f"    200 但响应结构不像 OpenAI chat：{body[:300]}")
        return 1
    print(f"    HTTP 200，回复：{content.strip().splitlines()[0][:80] if content.strip() else '(空)'}")
    print(f"    usage：{usage}")
    print("\n[结论] ✓ OpenAI 兼容，容器里的 llm.py 能直接用这组 base/model/key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
