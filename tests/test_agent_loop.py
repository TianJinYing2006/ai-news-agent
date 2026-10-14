"""ReAct 循环单测（实施计划阶段 3 验收）。

用假 client 覆盖：finish 出口、无 finish、工具错误回灌、finish 混合调用、
参数错误重试、连续重复、步数/token 上限、reasoning_content 回传与 trace 脱敏。

核心验收：**工具返回的结果真的影响模型的下一步工具选择**——
靠受控测试钉住，而不是"连跑几天看轨迹"。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from news_agent.agent.loop import AgentResult, run_agent
from news_agent.tools.control import finish as finish_tool
from news_agent.tools.registry import Tool, ToolRegistry

SYSTEM = "系统提示"
USER = "用户提示"
MODEL = "deepseek-flash"
TOOLS_SCHEMA: list[dict] = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
BRIEFING_PATH = "data/workspace/2026-10-14/briefing.md"


def _tool_call(name: str, arguments: dict | str | None = None, call_id: str = "call_1") -> dict:
    args = (
        arguments
        if isinstance(arguments, str)
        else json.dumps(arguments or {}, ensure_ascii=False)
    )
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def _response(
    tool_calls: list[dict] | None = None,
    content: str | None = None,
    reasoning: str | None = None,
    usage: tuple[int, int] | None = None,
) -> SimpleNamespace:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    usage_obj = (
        SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage_obj)


class FakeClient:
    """按脚本回放响应。脚本元素 = 响应对象，或 `callable(messages) -> 响应`。

    脚本最后一项会被重复使用（便于构造死循环场景）。
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, model: str, messages: list[dict], tools: list[dict]) -> SimpleNamespace:
        self.requests.append([dict(m) for m in messages])
        idx = min(len(self.requests) - 1, len(self.script) - 1)
        item = self.script[idx]
        if callable(item):
            return item(messages)
        return item


class BoomClient:
    """永远抛异常的 client，用来测 `error` 状态。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> SimpleNamespace:
        raise self.exc


def _registry(**handlers: Callable[..., str]) -> ToolRegistry:
    registry = ToolRegistry()

    def add(name: str, default: Callable[..., str]) -> None:
        registry.register(
            Tool(
                name=name,
                description=f"fake {name}",
                parameters={"type": "object", "properties": {}},
                fn=handlers.get(name, default),
            )
        )

    add("fetch_rss", lambda feed_url="": "RSS 结果：3 条条目")
    add("search_web", lambda query="": "搜索结果：2 条")
    add("write_file", lambda path="", content="": "已写入")
    registry.register(
        Tool(
            name="finish",
            description="宣告完成",
            parameters={"type": "object", "properties": {}},
            fn=finish_tool,
        )
    )
    return registry


def _run(fake: Any, **overrides: Any) -> AgentResult:
    kwargs: dict[str, Any] = {
        "system_prompt": SYSTEM,
        "user_prompt": USER,
        "tools_schema": TOOLS_SCHEMA,
        "registry": _registry(),
        "model": MODEL,
        "max_steps": 30,
        "token_budget": 800_000,
        "loop_window": 3,
    }
    kwargs.update(overrides)
    return run_agent(fake, **kwargs)


def _tools_in_trace(result: AgentResult) -> list[str]:
    return [e["tool"] for e in result.trace if "tool" in e]


# ---------- 成功路径与闭环验收 ----------


def test_finish_success_path() -> None:
    fake = FakeClient(
        [
            _response(
                [_tool_call("fetch_rss", {"feed_url": "https://feed.test/rss"})],
                usage=(100, 20),
            ),
            _response(
                [
                    _tool_call(
                        "finish",
                        {"summary": "今日 3 条", "briefing_path": BRIEFING_PATH},
                        call_id="call_2",
                    )
                ],
                usage=(50, 10),
            ),
        ]
    )
    result = _run(fake)
    assert result.status == "finished"
    assert result.steps == 2
    assert result.tool_call_count == 2  # fetch_rss + finish
    assert result.tokens_in == 150
    assert result.tokens_out == 30
    assert result.briefing_path == Path(BRIEFING_PATH)
    assert result.summary == "今日 3 条"
    assert result.error is None
    assert _tools_in_trace(result) == ["fetch_rss", "finish"]
    assert result.trace[-1]["event"] == "stop"
    assert result.trace[-1]["reason"] == "finished"


def test_tool_result_is_fed_back_and_changes_next_decision() -> None:
    """核心验收：结果回传给模型，模型据此换工具（fetch_rss 失败 → 改搜索）。"""

    def decide(messages: list[dict]) -> SimpleNamespace:
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert tool_msgs, "工具结果没有回传给模型"
        assert "[抓取失败]" in tool_msgs[-1]["content"]
        assert tool_msgs[-1]["tool_call_id"] == "call_rss"
        return _response(
            [_tool_call("search_web", {"query": "AI 新闻"}, call_id="call_search")]
        )

    fake = FakeClient(
        [
            _response(
                [_tool_call("fetch_rss", {"feed_url": "https://bad.test/rss"}, call_id="call_rss")]
            ),
            decide,
            _response(
                [_tool_call("finish", {"summary": "s", "briefing_path": BRIEFING_PATH})]
            ),
        ]
    )
    registry = _registry(fetch_rss=lambda feed_url="": "[抓取失败] 无法访问该源")
    result = _run(fake, registry=registry)

    assert result.status == "finished"
    assert _tools_in_trace(result) == ["fetch_rss", "search_web", "finish"]
    # 第三次请求里能看到 search_web 的结果（说明闭环真的在回传）
    assert any(
        m.get("role") == "tool" and "搜索结果" in m.get("content", "")
        for m in fake.requests[2]
    )


def test_tool_exception_is_fed_back() -> None:
    def raising(feed_url: str = "") -> str:
        raise RuntimeError("工具内部炸了")

    def decide(messages: list[dict]) -> SimpleNamespace:
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert "执行失败" in tool_msgs[-1]["content"]
        return _response(
            [_tool_call("finish", {"summary": "s", "briefing_path": BRIEFING_PATH})]
        )

    fake = FakeClient(
        [
            _response([_tool_call("fetch_rss", {"feed_url": "https://boom.test/rss"})]),
            decide,
        ]
    )
    result = _run(fake, registry=_registry(fetch_rss=raising))
    assert result.status == "finished"


# ---------- 不完整 / 格式错误 ----------


def test_incomplete_without_finish() -> None:
    fake = FakeClient([_response(content="我整理好了，就这样")])
    result = _run(fake)
    assert result.status == "incomplete"
    assert result.error and "finish" in result.error


def test_finish_must_be_alone_in_one_round() -> None:
    fake = FakeClient(
        [
            _response(
                [
                    _tool_call("fetch_rss", {"feed_url": "https://f.test/rss"}, call_id="c1"),
                    _tool_call(
                        "finish",
                        {"summary": "s", "briefing_path": BRIEFING_PATH},
                        call_id="c2",
                    ),
                ]
            ),
            _response(
                [
                    _tool_call(
                        "finish",
                        {"summary": "s", "briefing_path": BRIEFING_PATH},
                        call_id="c3",
                    )
                ]
            ),
        ]
    )
    result = _run(fake)
    assert result.status == "finished"
    format_errors = [
        m
        for m in fake.requests[1]
        if m.get("role") == "tool" and "唯一的工具调用" in m.get("content", "")
    ]
    assert len(format_errors) == 2  # 每个 tool_call 都要有对应的 tool 消息
    assert any(e.get("event") == "finish_not_alone" for e in result.trace)


def test_finish_bad_args_are_retried() -> None:
    fake = FakeClient(
        [
            _response([_tool_call("finish", {"summary": "s"}, call_id="c1")]),  # 缺 briefing_path
            _response(
                [
                    _tool_call(
                        "finish",
                        {"summary": "s", "briefing_path": BRIEFING_PATH},
                        call_id="c2",
                    )
                ]
            ),
        ]
    )
    result = _run(fake)
    assert result.status == "finished"
    errors = [m for m in fake.requests[1] if m.get("role") == "tool"]
    assert errors and "briefing_path" in errors[0]["content"]


# ---------- 三道硬闸 ----------


def test_loop_detected_before_executing_third_repeat() -> None:
    same = [_tool_call("fetch_rss", {"feed_url": "https://same.test/rss"})]
    fake = FakeClient([_response(same)])
    result = _run(fake, loop_window=3)
    assert result.status == "loop_detected"
    assert result.tool_call_count == 2  # 第 3 轮在执行前就被拦下
    assert any(e.get("event") == "loop_detected" for e in result.trace)


def test_max_steps_exceeded() -> None:
    fake = FakeClient(
        [
            _response([_tool_call("fetch_rss", {"feed_url": "https://a.test/rss"})]),
            _response([_tool_call("fetch_rss", {"feed_url": "https://b.test/rss"})]),
            _response([_tool_call("fetch_rss", {"feed_url": "https://c.test/rss"})]),
        ]
    )
    result = _run(fake, max_steps=3)
    assert result.status == "max_steps_exceeded"
    assert result.steps == 3
    assert result.tool_call_count == 3


def test_token_budget_exceeded() -> None:
    call = [
        _response(
            [_tool_call("fetch_rss", {"feed_url": "https://x.test/rss"})], usage=(30_000, 20_000)
        )
    ]
    fake = FakeClient(call)
    result = _run(fake, token_budget=60_000)
    assert result.status == "token_budget_exceeded"
    assert result.tokens_in == 60_000
    assert result.tokens_out == 40_000
    assert result.tool_call_count == 1  # 超预算的那一轮不再执行工具


# ---------- reasoning_content 与 trace 脱敏 ----------


def test_reasoning_content_preserved_but_never_in_trace() -> None:
    def decide(messages: list[dict]) -> SimpleNamespace:
        assistants = [m for m in messages if m.get("role") == "assistant"]
        assert any(m.get("reasoning_content") == "内部推理内容" for m in assistants), (
            "reasoning_content 没有随消息回传（DeepSeek thinking 会 400）"
        )
        return _response(
            [_tool_call("finish", {"summary": "s", "briefing_path": BRIEFING_PATH})]
        )

    fake = FakeClient(
        [
            _response(
                [_tool_call("fetch_rss", {"feed_url": "https://r.test/rss"})],
                reasoning="内部推理内容",
            ),
            decide,
        ]
    )
    result = _run(fake)
    assert result.status == "finished"
    serialized = json.dumps(result.trace, ensure_ascii=False)
    assert "reasoning_content" not in serialized
    assert "内部推理内容" not in serialized


def test_trace_redacts_key_shaped_arguments() -> None:
    fake = FakeClient(
        [
            _response(
                [
                    _tool_call(
                        "fetch_rss",
                        {"feed_url": "https://x.test/rss?key=sk-abcdef0123456789"},
                    )
                ]
            ),
            _response(
                [_tool_call("finish", {"summary": "s", "briefing_path": BRIEFING_PATH})]
            ),
        ]
    )
    result = _run(fake)
    assert result.status == "finished"
    serialized = json.dumps(result.trace, ensure_ascii=False)
    assert "sk-abcdef0123456789" not in serialized
    assert "sk-***" in serialized


# ---------- 模型调用失败 ----------


def test_client_error_returns_error_status() -> None:
    result = _run(BoomClient(RuntimeError("API 崩了")))
    assert result.status == "error"
    assert result.error and "API 崩了" in result.error
    assert result.trace[-1]["reason"] == "error"
