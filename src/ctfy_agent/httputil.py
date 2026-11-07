#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享 HTTP 传输助手：限读 + 重定向护栏。

解决审计里两类传输层问题：
  1. urllib 默认跟随重定向：MCP/LLM 客户端跨源重定向会外送 Authorization 头；
  2. http_request 的合规白名单只校验初始 URL，重定向目标不受校验。

用法：
  opener = build_opener(SameOriginRedirect(base_url))
  opener = build_opener(GuardedRedirect(check_fn))
"""

from __future__ import annotations

from urllib.parse import urlparse

import urllib.error
import urllib.request

MAX_READ_BYTES = 8 * 1024 * 1024  # MCP / LLM 响应体上限，防止恶意端点撑爆内存


def read_limited(response, limit: int = MAX_READ_BYTES) -> bytes:
    """读 response，最多 limit 字节（超出部分丢弃，返回时已读满 limit）。"""
    return response.read(limit)


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """只跟随同源重定向；跨源直接抛错，防止 Authorization 等凭据头被转发到第三方。"""

    def __init__(self, base_url: str):
        super().__init__()
        origin = urlparse(base_url)
        self._scheme = origin.scheme
        self._host = (origin.hostname or "").lower()
        self._port = origin.port

    def _same_origin(self, url: str) -> bool:
        target = urlparse(url)
        return (
            target.scheme == self._scheme
            and (target.hostname or "").lower() == self._host
            and target.port == self._port
        )

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._same_origin(newurl):
            raise urllib.error.URLError("跨源重定向已拦截（防止凭据头外送）")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """每次跳转都重新校验目标，且禁止 https→http 降级。check 抛异常即拦截。

    `capture`（可选）：跳转响应的头也会回调一次。很多站点把会话 cookie 种在
    302 上（登录/注册成功 → Location: /portal/ + Set-Cookie），而 urllib 跟随跳转后
    调用方只看到最终响应头——不在这里捞就会把会话丢掉，表现为「登录成功却一直
    是未登录页」。tools_local 用它把 302 上的 Set-Cookie 存进自己的 jar。

    `decorate`（可选）：跳转后的**新请求**建好后回调，用来补上依新 jar 计算的请求头
    （典型是 Cookie）。urllib 复制原请求头时还没有这个 cookie，不补则跳转后的页面
    仍是无会话的。
    """

    def __init__(self, check, capture=None, decorate=None):
        super().__init__()
        self._check = check
        self._capture = capture
        self._decorate = decorate

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(req.full_url).scheme == "https" and urlparse(newurl).scheme == "http":
            raise urllib.error.URLError("拒绝从 https 降级到 http 的重定向")
        self._check(newurl)
        if self._capture is not None and headers is not None:
            try:
                self._capture(req, headers)
            except Exception:  # noqa: BLE001 - 捞 cookie 失败不该击穿请求
                pass
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if self._decorate is not None and new_req is not None:
            try:
                self._decorate(new_req)
            except Exception:  # noqa: BLE001
                pass
        return new_req


def build_opener(handler: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """用自定义重定向处理器建 opener（build_opener 不会重复装默认重定向处理器）。"""
    return urllib.request.build_opener(handler)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随任何重定向：3xx 会作为 HTTPError 抛给调用方（code=3xx，headers 含 Location）。

    用于打开重定向、登录跳转链、SSRF 链等需要「看到原始 302 而不是最终页面」的场景。
    urllib 约定：redirect_request 返回 None 即不处理该重定向，最终按 HTTPError 上抛。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
