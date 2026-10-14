"""投递层：SMTP 发送（编排层职责，不注册为 Agent 工具）。"""

from .email import SendResult, send_briefing

__all__ = ["SendResult", "send_briefing"]
