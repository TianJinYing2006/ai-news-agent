"""Agent 层：提示词、模型客户端与手写 ReAct 循环。"""

from .client import build_client
from .loop import AgentResult, run_agent
from .prompt import build_prompts

__all__ = [
    "AgentResult",
    "build_client",
    "build_prompts",
    "run_agent",
]
