"""新闻获取工具：fetch_rss / search_web / fetch_url

这三个是让"新闻助手"真正成立的扩展工具。

设计要点：**失败要说得清楚**。返回给模型的错误文本必须包含"发生了什么 + 建议怎么办"，
模型才能据此换源或改策略——这正是闭环相对开环的价值（docs §5.1）。
比如 fetch_url 抽不到正文时，要明确提示"疑似付费墙或 JS 渲染"，而不是抛个异常。

出网统一经 `netguard`：SSRF 地址检查、逐跳复查重定向、超时与响应体上限
都在那里实现（URL 由模型提供，属于不可信输入）。
"""

from __future__ import annotations

import re
from functools import lru_cache

import feedparser
import trafilatura
from tavily import TavilyClient

from ..config import Config
from . import netguard

# 正文短于这个长度，判定为抓取失败（付费墙 / JS 渲染 / 反爬）
MIN_BODY_CHARS = 200
MAX_BODY_CHARS = 6_000


@lru_cache(maxsize=1)
def _cfg() -> Config:
    return Config.load()


def fetch_rss(feed_url: str, limit: int = 20) -> str:
    """抓取 RSS/Atom 源，返回条目列表。

    注意：feedparser 对失败源不会抛异常，而是返回空 entries 或带 bozo 标记，
    必须显式检查这两种情况，否则模型会以为"这个源今天没更新"。
    """
    limit = max(1, min(int(limit), 50))
    outcome = netguard.fetch(feed_url)
    if outcome.error:
        return (
            f"[抓取失败] {feed_url}\n"
            f"原因：{outcome.error}\n"
            "建议：换一个 RSS 源，或改用 search_web 搜索该话题。"
        )
    if outcome.truncated:
        return (
            f"[抓取失败] {feed_url} 的响应体超过大小上限，疑似异常源。\n"
            "建议：换源。"
        )

    feed = feedparser.parse(outcome.text)
    if getattr(feed, "bozo", 0) and not feed.entries:
        return (
            f"[解析失败] {feed_url} 返回的不是可解析的 RSS/Atom。"
            f"详情：{getattr(feed, 'bozo_exception', '未知')}\n"
            "建议：换源，或改用 search_web。"
        )
    if not feed.entries:
        return f"[空源] {feed_url} 可解析但没有任何条目。建议换源。"

    title = feed.feed.get("title", "（无标题）")
    lines = [f"来源：{title}  共 {len(feed.entries)} 条{'' if len(feed.entries) <= limit else f'，取前 {limit} 条'}："]

    for i, e in enumerate(feed.entries[:limit], start=1):
        lines.append(f"\n[{i}] {e.get('title', '（无标题）')}")
        lines.append(f"    链接：{e.get('link', '（无链接）')}")
        published = e.get("published") or e.get("updated") or "（无时间）"
        lines.append(f"    时间：{published}")
        summary = _clean(e.get("summary", ""))
        if summary:
            lines.append(f"    摘要：{summary[:300]}")

    return "\n".join(lines)


def search_web(query: str, max_results: int = 5) -> str:
    """用 Tavily 搜索网络，返回 LLM-ready 文本。"""
    max_results = max(1, min(int(max_results), 10))
    api_key = _cfg().tavily_api_key
    if not api_key:
        return (
            "[不可用] 未配置 TAVILY_API_KEY，无法联网搜索。\n"
            "建议：改用 fetch_rss 从 RSS 源获取内容。"
        )

    try:
        client = TavilyClient(api_key=api_key)
        data = client.search(query=query, max_results=max_results, search_depth="basic")
    except Exception as e:  # noqa: BLE001
        return (
            f"[搜索失败] 查询 {query!r} 时出错：{type(e).__name__}: {e}\n"
            "建议：换个说法重试，或改用 fetch_rss。"
        )

    results = data.get("results") or []
    if not results:
        return f"搜索 {query!r} 没有返回结果。建议换个关键词，或用 fetch_rss 兜底。"

    lines = [f"搜索 {query!r} 命中 {len(results)} 条："]
    for i, r in enumerate(results, start=1):
        lines.append(f"\n[{i}] {r.get('title', '（无标题）')}")
        lines.append(f"    链接：{r.get('url', '（无链接）')}")
        content = _clean(r.get("content", ""))
        if content:
            lines.append(f"    摘要：{content[:300]}")
    return "\n".join(lines)


def fetch_url(url: str) -> str:
    """抓取网页正文（自动抽取，去掉导航与广告）。"""
    if not url.startswith(("http://", "https://")):
        return f"[错误] URL 必须以 http:// 或 https:// 开头，收到：{url!r}"

    outcome = netguard.fetch(url)
    if outcome.error:
        return (
            f"[抓取失败] {url}\n"
            f"原因：{outcome.error}\n"
            "建议：改用 RSS 摘要，或换一篇来源。"
        )

    text = trafilatura.extract(outcome.text, include_comments=False, include_tables=False) or ""

    if len(text.strip()) < MIN_BODY_CHARS:
        return (
            f"[正文抓取失败] {url}\n"
            f"只抽出 {len(text.strip())} 字符（阈值 {MIN_BODY_CHARS}），"
            "疑似付费墙、JS 渲染或反爬。\n"
            "建议：改用 RSS 摘要，或用 search_web 找该事件的其它报道。"
        )

    body = text.strip()
    clipped = len(body) > MAX_BODY_CHARS
    note = "\n...(已截断)" if clipped else ""
    if outcome.truncated:
        note += "\n...(响应体超过上限，已截断读取)"
    return f"正文（来自 {url}）：\n" + body[:MAX_BODY_CHARS] + note


def _clean(html: str) -> str:
    """去掉 HTML 标签与多余空白，避免把标签喂给模型浪费 token。"""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()
