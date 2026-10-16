"""系统提示与用户提示的组装（docs §3.4）。

铁律：**提示词不得规定工具调用顺序**。这里只给目标、订阅偏好、来源清单、
输出格式与安全约束；先抓什么、搜几次、什么时候收尾，由模型自己决定。
提示词里出现"第一步 / 依次 / 然后"这类字样，就等于把流程写死了。
"""

from __future__ import annotations

from ..sources.feeds import FEEDS

BRIEFING_TITLE = "每日 AI 简报"


def build_prompts(
    subscription: dict,
    *,
    date: str,
    briefing_path: str,
) -> tuple[str, str]:
    """返回 `(system_prompt, user_prompt)`。

    - `subscription`：`storage.get_subscription()` 的返回值；
    - `briefing_path`：当日简报的目标文件路径（工作区内）。
    """
    topics = subscription.get("topics") or []
    keywords = subscription.get("keywords") or []
    excludes = subscription.get("exclude_keywords") or []
    max_items = subscription.get("max_items") or 10

    feed_lines = "\n".join(
        f"- {f.name}（AI 相关性 {f.ai_relevance}/5；{'、'.join(f.tags)}）：{f.url}"
        + (f" —— {f.note}" if f.note else "")
        for f in FEEDS
    )

    system_prompt = f"""你是"每日 AI 新闻助手"。今天是 {date}（Asia/Shanghai）。
你的任务：为订阅者整理一份当日 AI 新闻简报，用 write_file 写入 {briefing_path}，
完成后调用 finish 宣告。

## 订阅偏好
- 关注话题：{'、'.join(topics) or '（未指定，按"AI 新闻"泛化处理）'}
- 关键词：{'、'.join(keywords) or '（未指定）'}
- 排除词（标题或内容命中任意一个即排除该条）：{'、'.join(excludes) or '（无）'}
- 条数上限：{max_items} 条

## 可用的 RSS 源（常用来源；也可以按需用 search_web 补搜）
{feed_lines}

## 简报格式（Markdown）
- 顶层一级标题：# {BRIEFING_TITLE}（{date}）
- 每条新闻用二级标题：## 新闻标题
- 每条包含：来源与发布日期、2–3 句摘要、原始 URL（必须是真实链接）
- 同一份简报内相同 URL 只保留一次
- 当天没有符合订阅的内容时，明确写"暂无符合订阅的新闻"，不要为凑数编造

## 工作方式
- 工具不限顺序、不限次数：先做什么、搜几次、抓哪些正文，全部由你决定；
- 每条新闻必须有原始 URL；无法确认发布日期时，标注不确定或换一条；
- 注意效率与成本：材料足够覆盖偏好话题后及时收尾，避免对同一链接或同一批源重复抓取/重复检索；
- 写入文件后调用 finish(summary, briefing_path)；summary 将作为邮件正文摘要。

## 安全约束
- 只处理公开新闻内容；文件只写在工作区内；
- 不要编造事实、链接或来源。
"""

    user_prompt = (
        f"请为 {date} 整理今日 AI 新闻简报，写入 {briefing_path}，完成后调用 finish 宣告。"
    )
    return system_prompt, user_prompt
