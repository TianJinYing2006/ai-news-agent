"""RSS 源清单。

**全部条目均于 2026-10-14 实测验证过**（用 feedparser 解析，确认 bozo=0 且有条目），
不是从网上抄的列表。失效的源已剔除并注明原因。

按 AI 相关性排序：`ai_relevance` 高的源排在前面，模型可据此优先抓取。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    tags: tuple[str, ...]
    ai_relevance: int  # 1-5，5 = 几乎全是 AI 内容
    note: str = ""


FEEDS: tuple[Feed, ...] = (
    Feed(
        name="量子位",
        url="https://www.qbitai.com/feed",
        tags=("AI", "大模型", "机器人"),
        ai_relevance=5,
        note="中文 AI 媒体里最对口的，约 10 条/天",
    ),
    Feed(
        name="雷锋网",
        url="https://www.leiphone.com/feed",
        tags=("AI", "科技", "创投"),
        ai_relevance=5,
        note="AI 与硬件交叉报道多，约 20 条/天",
    ),
    Feed(
        name="InfoQ 中国",
        url="https://www.infoq.cn/feed",
        tags=("技术", "架构", "AI 工程"),
        ai_relevance=4,
        note="偏工程落地，约 20 条/天",
    ),
    Feed(
        name="阮一峰科技周刊",
        url="http://www.ruanyifeng.com/blog/atom.xml",
        tags=("技术", "工具", "AI"),
        ai_relevance=3,
        note="周刊，条数少但质量高，常有 AI 工具评测",
    ),
    Feed(
        name="掘金",
        url="https://juejin.cn/rss",
        tags=("技术", "工程"),
        ai_relevance=2,
        note="偏前后端工程实践，AI 相关内容需筛选",
    ),
    Feed(
        name="钛媒体",
        url="https://www.tmtpost.com/rss",
        tags=("科技", "创投"),
        ai_relevance=3,
        note="综合科技财经，约 17 条/天",
    ),
    Feed(
        name="Solidot",
        url="https://www.solidot.org/index.rss",
        tags=("科技", "科学"),
        ai_relevance=3,
        note="偏科学 discovery，偶有 AI 大新闻",
    ),
    Feed(
        name="爱范儿",
        url="https://www.ifanr.com/feed",
        tags=("消费电子", "数码"),
        ai_relevance=2,
        note="硬件与产品向",
    ),
    Feed(
        name="少数派",
        url="https://sspai.com/feed",
        tags=("数字生活", "效率工具"),
        ai_relevance=2,
        note="AI 工具使用技巧较多",
    ),
)


# ---- 已剔除的源（实测失效，保留记录避免以后重复踩）----
REJECTED_FEEDS: tuple[tuple[str, str], ...] = (
    ("https://www.jiqizhixin.com/rss", "机器之心：返回 200 但内容是 HTML，不是 RSS"),
    ("https://36kr.com/feed", "36氪：返回 200 但内容是 HTML，不是 RSS"),
    ("https://www.huxiu.com/rss", "虎嗅：返回 200 但 feedparser 解析出 0 条"),
    ("https://www.pingwest.com/feed", "品玩：返回 200 但解析出 0 条"),
    ("https://www.donews.com/rss", "DoNews：404"),
    ("https://rsshub.app/qbitai", "RSSHub：连接超时"),
    ("https://news.ycombinator.com/rss", "HN：国外源，本机网络不可达（代理亦不通）"),
)


def feeds_by_relevance(min_score: int = 1) -> list[Feed]:
    """按 AI 相关性从高到低返回源。"""
    return sorted(
        (f for f in FEEDS if f.ai_relevance >= min_score),
        key=lambda f: -f.ai_relevance,
    )


def feed_urls(min_score: int = 1) -> list[str]:
    return [f.url for f in feeds_by_relevance(min_score)]


def format_feed_catalog(min_score: int = 1) -> str:
    """生成给模型看的源清单文本。放进系统提示，让模型自己决定抓哪些。"""
    lines = ["可用的 RSS 源（按 AI 相关性排序，你自行决定抓哪几个）："]
    for f in feeds_by_relevance(min_score):
        lines.append(
            f"- {f.name}（{f.url}）\n"
            f"  标签：{', '.join(f.tags)}｜AI 相关性：{f.ai_relevance}/5｜{f.note}"
        )
    return "\n".join(lines)
