"""提示词与客户端工厂单测（实施计划阶段 2 的提示词部分）。

- 订阅偏好、排除词、条数上限被注入 system prompt；
- 全部 RSS 源出现在提示里；
- 提示词**没有规定工具执行顺序**（出现"第一步/依次/然后/接着"即判失败）；
- 未配置 key 时，构建客户端给出可读错误，而不是让 SDK 报 401。
"""

from __future__ import annotations

import pytest

from news_agent.agent.client import build_client
from news_agent.agent.prompt import build_prompts
from news_agent.config import LLMConfig
from news_agent.sources.feeds import FEEDS

SUBSCRIPTION = {
    "topics": ["大模型", "AI 应用"],
    "keywords": ["Agent", "开源"],
    "exclude_keywords": ["招聘", "广告"],
    "max_items": 6,
}


def _prompts() -> tuple[str, str]:
    return build_prompts(
        SUBSCRIPTION,
        date="2026-10-14",
        briefing_path="data/workspace/2026-10-14/briefing.md",
    )


def _llm_config(api_key: str) -> LLMConfig:
    return LLMConfig(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        thinking_enabled=True,
        max_steps=30,
        token_budget=800_000,
        loop_window=3,
    )


def test_prompt_injects_subscription() -> None:
    system, _ = _prompts()
    for word in ("大模型", "AI 应用", "Agent", "开源", "招聘", "广告", "6 条", "2026-10-14"):
        assert word in system, f"提示词缺少订阅信息：{word}"


def test_prompt_lists_all_feeds() -> None:
    system, _ = _prompts()
    for feed in FEEDS:
        assert feed.url in system, f"缺少源地址：{feed.name}"
        assert feed.name in system


def test_prompt_does_not_prescribe_tool_order() -> None:
    system, user = _prompts()
    assert "由你决定" in system  # 明确把顺序交给模型
    for forbidden in ("第一步", "依次", "然后", "接着"):
        assert forbidden not in system, f"system prompt 写死了流程：{forbidden}"
        assert forbidden not in user, f"user prompt 写死了流程：{forbidden}"


def test_user_prompt_only_states_goal() -> None:
    _, user = _prompts()
    assert "finish" in user
    assert "2026-10-14" in user
    assert len(user) < 200  # 一句话目标，不是步骤清单


def test_build_client_requires_key() -> None:
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        build_client(_llm_config(api_key=""))


def test_build_client_ok() -> None:
    client = build_client(_llm_config(api_key="test-key"))
    assert "api.deepseek.com" in str(client.base_url)
    client.close()
