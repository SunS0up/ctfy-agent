#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TPCTF 2026 智能体赛道解题框架。

对外只需要这几个入口：
    from ctfy_agent import Config, load, CtfyPlatform, DeepSeekLLM, Store, Solver
"""

from .config import PROJECT_ROOT, Config, load
from .llm import DeepSeekLLM, LLMError
from .mcp_client import McpHttpClient
from .platform import CtfyPlatform, PlatformError
from .solver import Solver
from .state import Store
from .tools_local import LocalTools, ToolError

__version__ = "0.2.0"

__all__ = [
    "PROJECT_ROOT",
    "Config",
    "load",
    "CtfyPlatform",
    "PlatformError",
    "DeepSeekLLM",
    "LLMError",
    "McpHttpClient",
    "Store",
    "Solver",
    "LocalTools",
    "ToolError",
    "__version__",
]
