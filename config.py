"""缅A决策看板 — 配置集中管理。"""

import os
from dataclasses import dataclass, field
from typing import Optional

from paths import data_dir as _data_dir


@dataclass
class Settings:
    # Server
    host: str = "0.0.0.0"
    port: int = 6799

    # Paths（数据目录唯一入口见 paths.py：可用 ETF_DATA_DIR 整体搬到用户目录/卷）
    data_dir: str = field(default_factory=_data_dir)

    # HTTP
    http_timeout: float = 6.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # 东方财富 token / referer
    em_token: str = "b2884a393a55266487d223057490786e"

    # Kronos 本地预测模型（facecat-kronos 仓库）
    kronos_repo: str = field(default_factory=lambda: os.getenv("KRONOS_REPO", "/Users/xinxin/workspace/facecat-kronos"))
    kronos_python: str = field(default_factory=lambda: os.getenv("KRONOS_PYTHON", "/Users/xinxin/workspace/facecat-kronos/.venv/bin/python3"))
    kronos_cache: str = field(default_factory=lambda: os.getenv("KRONOS_CACHE", "/Users/xinxin/workspace/facecat-kronos/model/hf-cache"))
    kronos_timeout: int = 180

    # LLM 决策（opencode zen / OpenAI 兼容端点）
    # 免费模型（*-free）走 zen 普通端点；Go 付费模型才走 zen/go。
    llm_api_base: str = field(default_factory=lambda: os.getenv("LLM_API_BASE", "https://opencode.ai/zen/v1"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "deepseek-v4.1-flash-free"))
    llm_timeout: int = 120
    llm_auth_json: str = field(default_factory=lambda: os.path.expanduser("~/.local/share/opencode/auth.json"))
    llm_auth_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    # REST 直连失败（免费额度 429/403）时，回落本机 opencode CLI 客户端通道
    llm_cli_fallback: bool = field(
        default_factory=lambda: os.getenv("LLM_CLI_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "off"))
    llm_cli_bin: str = field(default_factory=lambda: os.getenv("LLM_CLI_BIN", "opencode"))
    llm_cli_model: str = field(default_factory=lambda: os.getenv("LLM_CLI_MODEL", "opencode/deepseek-v4.1-flash-free"))
    llm_cli_timeout: int = field(default_factory=lambda: int(os.getenv("LLM_CLI_TIMEOUT", "240")))
    llm_cli_dir: str = field(default_factory=lambda: os.getenv("LLM_CLI_DIR", "/tmp/etf-llm-cli"))

    # 无风险利率（期权定价用，年化）
    risk_free_rate: float = 0.018

    # 监控轮询间隔（秒）。原先 3s 会让每轮上百个东财请求，触发限流/封禁，故放宽。
    poll_interval: int = 30
    # 交易时段
    session_am = ("09:30", "11:30")
    session_pm = ("13:00", "15:00")

    # 关键变盘时点（HH:MM）
    key_times: tuple = ("10:30", "11:30", "13:00", "14:30")

    @property
    def etf_root(self) -> str:
        return os.path.dirname(os.path.abspath(__file__))


settings = Settings()
