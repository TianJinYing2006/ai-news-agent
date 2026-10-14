"""集中配置。

设计原则：
1. 全部从环境变量读，不硬编码（代理端口历史上变过 7897 → 58928）。
2. **缺失的 key 不在此处报错** —— 工具层与单测要能在没有 key 的情况下跑，
   只有真正调用 LLM / SMTP 时才校验（见 require_* 方法）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
WORKSPACE_DIR = DATA_DIR / "workspace"
BRIEFINGS_DIR = DATA_DIR / "briefings"
DB_PATH = DATA_DIR / "news_agent.db"


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    thinking_enabled: bool
    # 三道硬闸，见 docs §5.3
    max_steps: int
    token_budget: int
    loop_window: int


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    user: str
    password: str
    from_name: str
    # 注意：收件人不在配置里 —— 取自网页身份设置的 profile.email（见实施计划 §3.5），
    # 避免在 .env 和网页两处各维护一份导致不一致。


@dataclass(frozen=True)
class Config:
    llm: LLMConfig
    tavily_api_key: str
    smtp: SMTPConfig

    @classmethod
    def load(cls, env_file: Path | None = None) -> Config:
        load_dotenv(env_file or PROJECT_ROOT / ".env")

        return cls(
            llm=LLMConfig(
                api_key=os.getenv("DEEPSEEK_API_KEY", ""),
                base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                thinking_enabled=os.getenv("DEEPSEEK_THINKING", "1") != "0",
                max_steps=int(os.getenv("MAX_STEPS", "30")),
                token_budget=int(os.getenv("TOKEN_BUDGET", "800000")),
                loop_window=int(os.getenv("LOOP_WINDOW", "3")),
            ),
            tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
            smtp=SMTPConfig(
                host=os.getenv("SMTP_HOST", "smtp.qq.com"),
                port=int(os.getenv("SMTP_PORT", "465")),
                user=os.getenv("SMTP_USER", ""),
                password=os.getenv("SMTP_PASS", ""),
                from_name=os.getenv("MAIL_FROM_NAME", "每日 AI 简报"),
            ),
        )


def ensure_dirs() -> None:
    """确保运行时目录存在。幂等，可重复调用。"""
    for d in (DATA_DIR, WORKSPACE_DIR, BRIEFINGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
