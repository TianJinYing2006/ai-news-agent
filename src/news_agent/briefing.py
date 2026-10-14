"""简报校验、渲染与归档（docs §3.3）。

模型写出的 Markdown 属于**外部内容**：校验通过才允许归档与发送；
渲染统一「关闭原始 HTML + nh3 allowlist 净化」，不直接回传浏览器，
也不自己手写不完整的 sanitizer。

简报采用固定轻量形状（便于校验和渲染）：

- 顶层一级标题；
- 每条新闻一个二级标题，包含来源/日期、摘要与原始 URL；
- 合法的零条简报：正文写明"暂无符合订阅的新闻"，不为凑数编造。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from html import escape
from pathlib import Path

import nh3
from markdown_it import MarkdownIt

EMPTY_MARKER = "暂无符合订阅的新闻"

_H1_RE = re.compile(r"^#\s+(.+)$")
_H2_RE = re.compile(r"^##\s+(.+)$")
_URL_RE = re.compile(r"https?://\S+")

# 渲染净化白名单：markdown-it 关闭原始 HTML 后仍要做第二道 allowlist 净化。
_ALLOWED_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "a",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "del",
    "code",
    "pre",
    "blockquote",
    "br",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}
_ALLOWED_ATTRS = {"a": {"href", "title"}}
_ALLOWED_SCHEMES = {"http", "https"}


@dataclass(frozen=True)
class BriefingCheck:
    ok: bool
    error: str | None = None
    title: str | None = None
    item_count: int = 0


def check_briefing(
    path: Path | str,
    *,
    workspace_root: Path | str,
    max_items: int,
) -> BriefingCheck:
    """校验 finish 给出的简报文件：必须在当日工作区内、存在且符合轻量形状。"""
    raw = Path(str(path))
    if not raw.is_absolute():
        return BriefingCheck(False, error=f"简报路径必须是绝对路径：{path}")

    resolved = raw.resolve()
    root = Path(str(workspace_root)).resolve()
    if not resolved.is_relative_to(root):
        return BriefingCheck(False, error=f"简报路径必须位于当日工作区内：{resolved}")
    if not resolved.is_file():
        return BriefingCheck(False, error=f"简报文件不存在：{resolved}")

    text = resolved.read_text(encoding="utf-8", errors="replace")
    return check_briefing_text(text, max_items=max_items)


def check_briefing_text(text: str, *, max_items: int) -> BriefingCheck:
    """对简报正文做轻量形状检查（标题、条目、URL、条数上限）。"""
    if not text.strip():
        return BriefingCheck(False, error="简报内容为空")

    title: str | None = None
    sections: list[list[str]] = []
    current: list[str] | None = None

    for line in text.splitlines():
        h1 = _H1_RE.match(line)
        if h1 is not None and title is None:
            title = h1.group(1).strip()
            continue
        if _H2_RE.match(line):
            current = [line]
            sections.append(current)
            continue
        if current is not None:
            current.append(line)

    if title is None:
        return BriefingCheck(False, error="简报缺少顶层一级标题（# ……）")

    if not sections:
        if EMPTY_MARKER in text:
            return BriefingCheck(True, title=title, item_count=0)
        return BriefingCheck(False, error=f"没有二级标题条目，也未写明“{EMPTY_MARKER}”")

    if len(sections) > max_items:
        return BriefingCheck(
            False, error=f"条目数 {len(sections)} 超过订阅上限 {max_items}", item_count=len(sections)
        )

    for idx, section in enumerate(sections, start=1):
        if not _URL_RE.search("\n".join(section)):
            return BriefingCheck(False, error=f"第 {idx} 条新闻缺少原始 URL")

    return BriefingCheck(True, title=title, item_count=len(sections))


@lru_cache(maxsize=1)
def _markdown() -> MarkdownIt:
    """commonmark 预设 + 关闭原始 HTML —— 第一道防线。"""
    return MarkdownIt("commonmark", {"html": False})


def render_body_html(md_text: str) -> str:
    """Markdown → 净化后的 HTML 片段（nh3 allowlist 是第二道防线）。"""
    raw_html = _markdown().render(md_text)
    return nh3.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes=_ALLOWED_SCHEMES,
    )


def render_document_html(md_text: str, *, title: str) -> str:
    """生成完整 HTML 文档（网页查看与邮件正文共用）。"""
    body = render_body_html(md_text)
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{escape(title)}</title>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def archive_briefing(
    md_text: str,
    *,
    date: str,
    title: str,
    briefings_dir: Path | str,
) -> tuple[Path, Path]:
    """把已验证的简报原子写入 `briefings_dir/YYYY-MM-DD.md|.html`，返回两个路径。"""
    _validate_date(date)
    target_dir = Path(str(briefings_dir))
    target_dir.mkdir(parents=True, exist_ok=True)
    md_path = target_dir / f"{date}.md"
    html_path = target_dir / f"{date}.html"

    _atomic_write_text(md_path, md_text)
    _atomic_write_text(html_path, render_document_html(md_text, title=title))
    return md_path, html_path


def _atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再 `os.replace`，避免半截文件被网页/邮件读到。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _validate_date(date: str) -> None:
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(f"日期必须形如 YYYY-MM-DD，收到：{date!r}") from None
