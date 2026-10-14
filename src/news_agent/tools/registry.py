"""工具注册与分派。

三条契约，都对应文档里的设计决策：

1. **每个工具返回 str**，直接作为 `role=tool` 的 content 回传给模型。
2. **工具出错不抛出异常到循环外**，而是把错误文本返回给模型，让它自己看见并纠正。
   这是闭环相对开环的核心优势（docs §5.1）——模型能看到自己动作的结果。
3. **`arguments` 不保证是合法 JSON**（docs §3 坑 4），解析失败时把错误回传让模型重试。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]

    def to_schema(self) -> dict[str, Any]:
        """转成 DeepSeek / OpenAI 的 tools 数组元素。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具重名：{tool.name}")
        self._tools[tool.name] = tool
        return tool

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.to_schema() for t in self._tools.values()]

    def dispatch(self, name: str, arguments_json: str) -> str:
        """执行工具，永不向外抛异常。"""
        tool = self._tools.get(name)
        if tool is None:
            return (
                f"[错误] 未知工具 {name!r}。可用工具：{', '.join(self.names())}\n"
                "请从可用工具中选择一个重新调用。"
            )

        raw = (arguments_json or "").strip()
        if not raw:
            args: dict[str, Any] = {}
        else:
            try:
                args = json.loads(raw)
            except json.JSONDecodeError as e:
                # 坑 4：让模型看见自己的格式错误并自纠
                return (
                    f"[错误] 工具参数不是合法 JSON：{e}\n"
                    f"你传入的内容是：{raw!r}\n"
                    f"请重新调用 {name}，把参数改成合法 JSON。"
                )

        if not isinstance(args, dict):
            return (
                f"[错误] {name} 的参数必须是 JSON 对象，"
                f"实际收到 {type(args).__name__}。请重试。"
            )

        try:
            return tool.fn(**args)
        except TypeError as e:
            return f"[错误] 调用 {name} 的参数不匹配：{e}。请核对参数名后重试。"
        except Exception as e:  # noqa: BLE001 - 故意兜住一切，交给模型处理
            return f"[错误] 工具 {name} 执行失败：{type(e).__name__}: {e}"
