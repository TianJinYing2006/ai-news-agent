"""手写 ReAct / function-calling 循环（docs §3.3）。

这是项目的核心验收点：**工具调用顺序真的由 LLM 决定**。
循环只负责：把完整模型响应回传、执行它选择的工具、把结果交回去，
并守住三道硬闸（max_steps / token 预算 / 连续重复调用）。
在提示词与这里都不考虑"先抓什么、再做什么"——那是模型自己的决定。

几个必须守住的契约（都有测试钉住）：

- 每轮把**完整 assistant 响应**追加回消息历史（`model_dump` 而不是手工重建），
  DeepSeek thinking 模式的 `reasoning_content` 必须随消息回传，否则 400；
- 工具结果与错误都作为 `role=tool` 消息交回模型——闭环的价值所在；
- `finish` 是唯一成功出口，必须是该轮唯一的 tool call；
  模型不调用 finish 就停止 → `incomplete`，绝不能当成简报成功；
- 任一硬闸触发 = 失败返回并记录，**不产出降级/编造简报**；
- trace 只记录脱敏后的参数与结果摘要，不落 `reasoning_content`、key 或完整正文。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..tools.control import FINISH_TOOL_NAME
from ..tools.registry import ToolRegistry

TZ = ZoneInfo("Asia/Shanghai")

MAX_ARG_CHARS = 500
MAX_RESULT_CHARS = 200
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


@dataclass
class AgentResult:
    status: str  # finished | max_steps_exceeded | token_budget_exceeded |
    #              loop_detected | incomplete | error
    steps: int
    tool_call_count: int
    tokens_in: int
    tokens_out: int
    trace: list[dict]
    briefing_path: Path | None
    summary: str | None
    error: str | None = None


def run_agent(
    client: Any,
    *,
    system_prompt: str,
    user_prompt: str,
    tools_schema: list[dict],
    registry: ToolRegistry,
    model: str,
    max_steps: int,
    token_budget: int,
    loop_window: int,
) -> AgentResult:
    """执行 ReAct 循环。`client` 是 OpenAI 兼容客户端（测试用假 client）。"""
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    trace: list[dict] = []
    steps = 0
    tool_call_count = 0
    tokens_in = 0
    tokens_out = 0
    last_fp: str | None = None
    loop_streak = 0

    def finish(
        status: str,
        *,
        briefing_path: Path | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> AgentResult:
        trace.append(
            {
                "ts": _now_iso(),
                "event": "stop",
                "reason": status,
                "steps": steps,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            }
        )
        return AgentResult(
            status=status,
            steps=steps,
            tool_call_count=tool_call_count,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            trace=trace,
            briefing_path=briefing_path,
            summary=summary,
            error=error,
        )

    while steps < max_steps:
        steps += 1

        try:
            response = client.chat.completions.create(
                model=model, messages=messages, tools=tools_schema
            )
        except Exception as e:  # noqa: BLE001 - 模型调用失败也必须是可记录的失败，而不是崩溃
            return finish("error", error=f"模型调用失败：{type(e).__name__}: {e}")

        message = _message_to_dict(response.choices[0].message)
        messages.append(message)

        usage = getattr(response, "usage", None)
        if usage is not None:
            tokens_in += getattr(usage, "prompt_tokens", 0) or 0
            tokens_out += getattr(usage, "completion_tokens", 0) or 0
        if tokens_in + tokens_out > token_budget:
            return finish(
                "token_budget_exceeded",
                error=f"token 预算超限：{tokens_in + tokens_out} > {token_budget}",
            )

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            # 模型不再请求工具、也没有 finish —— 不能当成成功
            return finish("incomplete", error="模型未调用 finish 就停止了")

        names = [tc["function"]["name"] for tc in tool_calls]

        # 连续重复调用检测：同一轮工具+参数连续出现 loop_window 次即判死循环
        fp = json.dumps(
            [[tc["function"]["name"], tc["function"].get("arguments", "")] for tc in tool_calls],
            ensure_ascii=False,
        )
        if fp == last_fp:
            loop_streak += 1
        else:
            loop_streak = 1
            last_fp = fp
        if loop_streak >= loop_window:
            trace.append({"ts": _now_iso(), "event": "loop_detected", "tools": names})
            return finish(
                "loop_detected",
                error=f"连续 {loop_window} 轮相同的工具调用：{'、'.join(names)}",
            )

        if FINISH_TOOL_NAME in names:
            if len(tool_calls) > 1:
                # finish 不允许和其它调用混在同一轮：让模型重新决定
                for tc in tool_calls:
                    _append_tool_message(
                        messages,
                        tc,
                        "[错误] finish 必须是该轮唯一的工具调用。"
                        "请重新决定：要么单独调用 finish，要么先处理其它工具调用。",
                    )
                trace.append(
                    {"ts": _now_iso(), "step": steps, "event": "finish_not_alone", "tools": names}
                )
                continue

            tc = tool_calls[0]
            raw_args = tc["function"].get("arguments", "")
            args, arg_error = _parse_finish_args(raw_args)
            if arg_error is not None:
                _append_tool_message(messages, tc, f"[错误] {arg_error}。请修正后重新调用 finish。")
                trace.append(
                    {
                        "ts": _now_iso(),
                        "step": steps,
                        "tool": FINISH_TOOL_NAME,
                        "args": _redact_args(raw_args),
                        "result": f"[错误] {arg_error}",
                    }
                )
                continue

            executed = registry.dispatch(FINISH_TOOL_NAME, raw_args)
            tool_call_count += 1
            trace.append(
                {
                    "ts": _now_iso(),
                    "step": steps,
                    "tool": FINISH_TOOL_NAME,
                    "args": _redact_args(raw_args),
                    "result": _summarize(executed),
                }
            )
            return finish(
                "finished",
                briefing_path=Path(args["briefing_path"]),
                summary=args["summary"].strip(),
            )

        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"].get("arguments", "")
            started = time.perf_counter()
            executed = registry.dispatch(name, raw_args)
            duration_ms = int((time.perf_counter() - started) * 1000)
            tool_call_count += 1
            _append_tool_message(messages, tc, executed)
            trace.append(
                {
                    "ts": _now_iso(),
                    "step": steps,
                    "tool": name,
                    "args": _redact_args(raw_args),
                    "result": _summarize(executed),
                    "duration_ms": duration_ms,
                }
            )

    return finish("max_steps_exceeded", error=f"达到步数上限 {max_steps}，仍未调用 finish")


def _message_to_dict(message: Any) -> dict:
    """把 SDK 响应消息转成可直接回传的消息 dict。

    - 优先 `model_dump`：完整保留响应字段（含 thinking 的 `reasoning_content`），
      不手工重建 assistant 消息；
    - 保证 `content` 键存在：带 tool_calls 的轮次 content=None，但键要留下来。
    """
    if isinstance(message, dict):
        data = dict(message)
    else:
        data = message.model_dump(exclude_none=True)
        data["content"] = message.content
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning is not None:
            data["reasoning_content"] = reasoning
    data.setdefault("role", "assistant")
    return data


def _append_tool_message(messages: list[dict], tool_call: dict, content: str) -> None:
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call.get("id") or "",
            "content": content,
        }
    )


def _parse_finish_args(raw: str) -> tuple[dict, None] | tuple[None, str]:
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return None, f"finish 参数不是合法 JSON：{e}"
    if not isinstance(args, dict):
        return None, "finish 参数必须是 JSON 对象"
    if not isinstance(args.get("summary"), str) or not args["summary"].strip():
        return None, "finish 缺少非空 summary（将作为邮件摘要）"
    path = args.get("briefing_path")
    if not isinstance(path, str) or not path.strip():
        return None, "finish 缺少非空 briefing_path（简报文件路径）"
    return args, None


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _redact_args(raw: str) -> str:
    """参数脱敏：截断 + 打掉可能的 key 形态字符串。"""
    return _SECRET_RE.sub("sk-***", raw or "")[:MAX_ARG_CHARS]


def _summarize(text: str) -> str:
    """结果摘要：折叠空白、脱敏、截断——trace 里不落完整正文。"""
    cleaned = _SECRET_RE.sub("sk-***", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > MAX_RESULT_CHARS:
        return cleaned[:MAX_RESULT_CHARS] + "…"
    return cleaned
