#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置加载：config/agent.json → .env → 环境变量（后者覆盖前者）。

所有可调项都在 config/agent.json 里有一份默认值，环境变量用于临时覆盖，
比赛机上只需要放一个 .env（令牌 + API Key），代码不用改。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "agent.json"
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULTS = {
    # 平台侧
    "mcp_url": "https://ctfy.tsinghua.edu.cn/api/v1/mcp/",
    "timeout": 120.0,
    # 模型侧（DeepSeek V4.1 Flash，OpenAI 兼容接口）
    "model": "deepseek-v4.1-flash",
    "llm_base_url": "https://api.deepseek.com/v1",
    "temperature": 0.2,
    "max_tokens": 4096,
    # 智能体行为
    "max_steps": 12,
    "submit_limit": 3,
    "max_calls_per_step": 8,
    "instance_ops_limit": 5,
    "oracle_limit": 50,
    "allow_shell": False,
    "debug": False,
    # 费用/用量上限（硬刹车，防止跑飞烧钱）
    "max_tokens_per_challenge": 300000,   # 单题累计 token 上限（跨进程继承，重跑接着算）
    "max_llm_calls": 20,                  # 单题累计模型调用次数上限
    "max_context_tokens": 16000,          # 单次请求上下文滑窗（估算 token），超窗丢最旧步骤
    "max_cost": 0.0,                      # 单题金额上限（0=不限；需同时配单价才生效）
    "price_input_per_mtok": 0.0,          # 输入 token 单价（每百万 token）
    "price_output_per_mtok": 0.0,         # 输出 token 单价（每百万 token）
    # 产物
    "runs_dir": "runs",
}

ENV_MAP = {
    "CTFY_MCP_URL": "mcp_url",
    "AGENT_TIMEOUT": "timeout",
    "DEEPSEEK_MODEL": "model",
    "DEEPSEEK_BASE_URL": "llm_base_url",
    "DEEPSEEK_TEMPERATURE": "temperature",
    "DEEPSEEK_MAX_TOKENS": "max_tokens",
    "AGENT_MAX_STEPS": "max_steps",
    "AGENT_SUBMIT_LIMIT": "submit_limit",
    "AGENT_MAX_CALLS_PER_STEP": "max_calls_per_step",
    "AGENT_INSTANCE_OPS_LIMIT": "instance_ops_limit",
    "AGENT_ORACLE_LIMIT": "oracle_limit",
    "AGENT_ALLOW_SHELL": "allow_shell",
    "AGENT_DEBUG": "debug",
    "AGENT_MAX_TOKENS_PER_CHALLENGE": "max_tokens_per_challenge",
    "AGENT_MAX_LLM_CALLS": "max_llm_calls",
    "AGENT_MAX_CONTEXT_TOKENS": "max_context_tokens",
    "AGENT_MAX_COST": "max_cost",
    "AGENT_PRICE_INPUT_PER_MTOK": "price_input_per_mtok",
    "AGENT_PRICE_OUTPUT_PER_MTOK": "price_output_per_mtok",
    "AGENT_RUNS_DIR": "runs_dir",
}

TRUTHY = {"1", "true", "yes", "y", "on"}


def load_dotenv(path: Path, override: bool = False) -> None:
    """极简 .env 读取，避免为了一个文件引入 python-dotenv。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def _coerce(value, default):
    if isinstance(default, bool):
        return str(value).strip().lower() in TRUTHY
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return str(value)


@dataclass
class Config:
    mcp_url: str = DEFAULTS["mcp_url"]
    timeout: float = DEFAULTS["timeout"]

    model: str = DEFAULTS["model"]
    llm_base_url: str = DEFAULTS["llm_base_url"]
    temperature: float = DEFAULTS["temperature"]
    max_tokens: int = DEFAULTS["max_tokens"]

    max_steps: int = DEFAULTS["max_steps"]
    submit_limit: int = DEFAULTS["submit_limit"]
    max_calls_per_step: int = DEFAULTS["max_calls_per_step"]
    instance_ops_limit: int = DEFAULTS["instance_ops_limit"]
    oracle_limit: int = DEFAULTS["oracle_limit"]
    allow_shell: bool = DEFAULTS["allow_shell"]
    debug: bool = DEFAULTS["debug"]

    max_tokens_per_challenge: int = DEFAULTS["max_tokens_per_challenge"]
    max_llm_calls: int = DEFAULTS["max_llm_calls"]
    max_context_tokens: int = DEFAULTS["max_context_tokens"]
    max_cost: float = DEFAULTS["max_cost"]
    price_input_per_mtok: float = DEFAULTS["price_input_per_mtok"]
    price_output_per_mtok: float = DEFAULTS["price_output_per_mtok"]

    runs_dir: str = DEFAULTS["runs_dir"]

    # 凭据（只从环境变量 / .env 来，不写进 JSON）
    token: str = ""
    llm_api_key: str = ""

    extra: dict = field(default_factory=dict)

    # ---------- 派生路径 ----------

    @property
    def runs_path(self) -> Path:
        path = Path(self.runs_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path

    def run_dir(self, run_id: str) -> Path:
        # 审计 L1：run_id 只允许字母数字与 . _ -，且不得是 . / ..，杜绝路径穿越
        if not run_id or run_id in (".", "..") or ".." in run_id:
            raise ValueError(f"非法的 run_id：{run_id!r}（禁止为空、. 或包含 ..）")
        if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for ch in run_id):
            raise ValueError(f"非法的 run_id：{run_id!r}（只允许字母、数字与 . _ -）")
        return self.runs_path / run_id

    # ---------- 检查 ----------

    def missing(self) -> list[str]:
        problems = []
        if not self.token:
            problems.append("CTFY_TOKEN（平台令牌，设置 → API 令牌）")
        if not self.llm_api_key:
            problems.append("DEEPSEEK_API_KEY（DeepSeek V4.1 Flash 的 Key）")
        return problems

    def masked(self) -> dict:
        def mask(value: str) -> str:
            if not value:
                return "(未设置)"
            # 只暴露前 4 个字符用于识别（pf_ / sk- 前缀），不露尾部
            return value[:4] + "***" if len(value) > 4 else "***"

        return {
            "mcp_url": self.mcp_url,
            "token": mask(self.token),
            "model": self.model,
            "llm_base_url": self.llm_base_url,
            "llm_api_key": mask(self.llm_api_key),
            "max_steps": self.max_steps,
            "submit_limit": self.submit_limit,
            "max_calls_per_step": self.max_calls_per_step,
            "instance_ops_limit": self.instance_ops_limit,
            "oracle_limit": self.oracle_limit,
            "allow_shell": self.allow_shell,
            "max_tokens_per_challenge": self.max_tokens_per_challenge,
            "max_llm_calls": self.max_llm_calls,
            "max_context_tokens": self.max_context_tokens,
            "max_cost": self.max_cost,
            "price_input_per_mtok": self.price_input_per_mtok,
            "price_output_per_mtok": self.price_output_per_mtok,
            "runs_dir": str(self.runs_path),
        }


def load(config_path: Path | None = None, use_env_file: bool = True) -> Config:
    if use_env_file:
        load_dotenv(ENV_FILE)

    raw = dict(DEFAULTS)
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    extra: dict = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for key, value in data.items():
            if key in raw:
                raw[key] = value
            else:
                extra[key] = value

    for env_key, field_name in ENV_MAP.items():
        if env_key in os.environ and os.environ[env_key].strip():
            raw[field_name] = _coerce(os.environ[env_key], DEFAULTS[field_name])

    return Config(
        mcp_url=raw["mcp_url"],
        timeout=float(raw["timeout"]),
        model=raw["model"],
        llm_base_url=raw["llm_base_url"],
        temperature=float(raw["temperature"]),
        max_tokens=int(raw["max_tokens"]),
        max_steps=int(raw["max_steps"]),
        submit_limit=int(raw["submit_limit"]),
        max_calls_per_step=int(raw["max_calls_per_step"]),
        instance_ops_limit=int(raw["instance_ops_limit"]),
        oracle_limit=int(raw["oracle_limit"]),
        allow_shell=bool(raw["allow_shell"]),
        debug=bool(raw["debug"]),
        max_tokens_per_challenge=int(raw["max_tokens_per_challenge"]),
        max_llm_calls=int(raw["max_llm_calls"]),
        max_context_tokens=int(raw["max_context_tokens"]),
        max_cost=float(raw["max_cost"]),
        price_input_per_mtok=float(raw["price_input_per_mtok"]),
        price_output_per_mtok=float(raw["price_output_per_mtok"]),
        runs_dir=str(raw["runs_dir"]),
        token=os.environ.get("CTFY_TOKEN", "").strip(),
        llm_api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        extra=extra,
    )
