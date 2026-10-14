"""把 9 个工具装配成一个注册表。

`description` 的写法是有讲究的：模型是照着描述决定"什么时候该用、怎么用"的。
所以每条描述都写了三件事——能干什么、什么时候该用、失败了该怎么办。
"""

from __future__ import annotations

from .control import finish
from .filesystem import list_dir, read_file, search_content, write_file
from .news import fetch_rss, fetch_url, search_web
from .registry import Tool, ToolRegistry
from .shell import bash


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()

    # ---------- 需求要求的 5 个 ----------
    reg.register(
        Tool(
            name="list_dir",
            description=(
                "列出目录内容。用于查看工作区里已缓存了哪些文件、有哪些历史简报可查。"
                "相对路径以当日工作区为基准；也可用绝对路径访问 data/ 下的其它目录。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径，留空表示当日工作区。",
                    }
                },
                "required": [],
            },
            fn=list_dir,
        )
    )

    reg.register(
        Tool(
            name="read_file",
            description=(
                "读取文件正文。用于读订阅偏好、读已抓取的新闻原文、读历史简报。"
                "超长文件会自动截断。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径。"}
                },
                "required": ["path"],
            },
            fn=read_file,
        )
    )

    reg.register(
        Tool(
            name="search_content",
            description=(
                "在目录下按关键词搜索文件内容，返回命中的片段（不是整篇）。"
                "用于：① 按订阅关键词筛选已抓取的新闻；"
                "② 检索 data/briefings/ 判断某条新闻是不是前几天报道过的事件的后续。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要搜索的关键词。"},
                    "dir": {
                        "type": "string",
                        "description": "搜索范围目录，留空表示当日工作区。",
                    },
                },
                "required": ["keyword"],
            },
            fn=search_content,
        )
    )

    reg.register(
        Tool(
            name="write_file",
            description="写入文件。用于保存抓取到的素材、简报草稿和最终简报。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径。"},
                    "content": {"type": "string", "description": "要写入的完整内容。"},
                },
                "required": ["path", "content"],
            },
            fn=write_file,
        )
    )

    reg.register(
        Tool(
            name="bash",
            description=(
                "通过真实 Bash 子进程执行受限命令。仅允许 ls/cat/grep/wc/date/head/tail；"
                "文件参数限于 data/，禁止管道、重定向、命令替换和命令拼接。"
                "用于快速查看与统计工作区文件。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令。"}
                },
                "required": ["command"],
            },
            fn=bash,
        )
    )

    # ---------- 新闻专属 4 个 ----------
    reg.register(
        Tool(
            name="fetch_rss",
            description=(
                "抓取一个 RSS/Atom 源，返回条目的标题、链接、时间与摘要。"
                "这是获取新闻的主渠道。若某个源失败（返回[抓取失败]或[空源]），"
                "换其它源或改用 search_web。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "feed_url": {"type": "string", "description": "RSS/Atom 源的完整 URL。"},
                    "limit": {
                        "type": "integer",
                        "description": "最多返回多少条，默认 20，上限 50。",
                    },
                },
                "required": ["feed_url"],
            },
            fn=fetch_rss,
        )
    )

    reg.register(
        Tool(
            name="search_web",
            description=(
                "联网搜索一个话题，返回相关网页的标题、链接与摘要。"
                "用于：① 补充 RSS 没覆盖到的内容；② 某个源失效时兜底；"
                "③ 主动追踪某个你判断值得深挖的话题。免费额度有限，按需使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词。"},
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回几条，默认 5，上限 10。",
                    },
                },
                "required": ["query"],
            },
            fn=search_web,
        )
    )

    reg.register(
        Tool(
            name="fetch_url",
            description=(
                "抓取一个网页的正文（自动去掉导航、广告等噪声）。"
                "若返回[正文抓取失败]，说明疑似付费墙或 JS 渲染，"
                "应改用 RSS 摘要或换一篇报道。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要抓取的网页 URL。"}
                },
                "required": ["url"],
            },
            fn=fetch_url,
        )
    )

    reg.register(
        Tool(
            name="finish",
            description=(
                "宣告简报已完成。必须先把简报写入文件再调用本工具。"
                "邮件发送由编排层负责，你不需要也不应该自己发邮件。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "一两句话概括今天的简报，会放进邮件开头。",
                    },
                    "briefing_path": {"type": "string", "description": "简报文件的路径。"},
                },
                "required": ["summary", "briefing_path"],
            },
            fn=finish,
        )
    )

    return reg
