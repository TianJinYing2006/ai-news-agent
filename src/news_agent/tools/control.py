"""终止工具：finish

模型调用它来宣告"简报写完了"。这是循环的正常出口。

注意 `send_email` **不在工具列表里** —— docs §4：邮件是不可撤销的外部副作用，
交给编排层而不是模型。模型只负责把简报写到文件，然后喊停。
"""

from __future__ import annotations

FINISH_TOOL_NAME = "finish"


def finish(summary: str, briefing_path: str) -> str:
    """宣告任务完成。summary 给编排层写邮件正文用，briefing_path 指向简报文件。"""
    return (
        f"任务完成。\n简报路径：{briefing_path}\n摘要：{summary}\n"
        "编排层接下来会负责渲染、落库和发送邮件。"
    )
