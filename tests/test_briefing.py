"""简报校验 / 渲染净化 / 归档单测（实施计划阶段 4 的校验与渲染部分）。

覆盖：轻量形状校验（标题、条目、URL、条数上限、零条合法）、
路径边界（工作区外/相对路径/不存在）、渲染净化（原始 HTML、javascript: 链接）、
原子归档与日期注入防护。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from news_agent import briefing

GOOD_MD = """# 每日 AI 简报（2026-10-14）

## 某模型发布新版本

- 来源：量子位 · 2026-10-14
- 摘要：模型能力提升，推理成本下降。
- 原文：https://example.com/news/1

## 另一条新闻

来源：InfoQ · 2026-10-13

摘要：foo

https://example.com/news/2
"""

EMPTY_MD = "# 每日 AI 简报（2026-10-14）\n\n暂无符合订阅的新闻。\n"


# ---------- 形状校验 ----------


def test_valid_briefing_passes() -> None:
    check = briefing.check_briefing_text(GOOD_MD, max_items=10)
    assert check.ok
    assert check.item_count == 2
    assert check.title and "2026-10-14" in check.title


def test_zero_item_briefing_allowed() -> None:
    check = briefing.check_briefing_text(EMPTY_MD, max_items=10)
    assert check.ok
    assert check.item_count == 0


def test_empty_text_rejected() -> None:
    assert not briefing.check_briefing_text("   \n", max_items=10).ok


def test_missing_title_rejected() -> None:
    check = briefing.check_briefing_text("## 条目\n\nhttps://example.com/1\n", max_items=10)
    assert not check.ok
    assert check.error and "一级标题" in check.error


def test_item_without_url_rejected() -> None:
    check = briefing.check_briefing_text("# 标题\n\n## 没链接的条目\n\n来源：某处\n", max_items=10)
    assert not check.ok
    assert check.error and "URL" in check.error


def test_count_over_max_rejected() -> None:
    check = briefing.check_briefing_text(GOOD_MD, max_items=1)
    assert not check.ok
    assert check.error and "上限" in check.error


def test_no_items_without_marker_rejected() -> None:
    check = briefing.check_briefing_text("# 标题\n\n今天什么都没写\n", max_items=10)
    assert not check.ok
    assert check.error and "暂无" in check.error


# ---------- 路径边界 ----------


def test_check_briefing_requires_absolute_path() -> None:
    check = briefing.check_briefing("briefing.md", workspace_root=Path("x"), max_items=10)
    assert not check.ok
    assert check.error and "绝对路径" in check.error


def test_check_briefing_rejects_path_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(GOOD_MD, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    check = briefing.check_briefing(outside, workspace_root=workspace, max_items=10)
    assert not check.ok
    assert check.error and "工作区" in check.error


def test_check_briefing_missing_file(tmp_path: Path) -> None:
    check = briefing.check_briefing(tmp_path / "nope.md", workspace_root=tmp_path, max_items=10)
    assert not check.ok
    assert check.error and "不存在" in check.error


def test_check_briefing_reads_valid_file(tmp_path: Path) -> None:
    f = tmp_path / "briefing.md"
    f.write_text(GOOD_MD, encoding="utf-8")
    check = briefing.check_briefing(f, workspace_root=tmp_path, max_items=10)
    assert check.ok
    assert check.item_count == 2


# ---------- 渲染净化 ----------


def test_render_strips_raw_html() -> None:
    md = "# 标题\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n\n正文\n"
    out = briefing.render_body_html(md)
    # 原始 HTML 被 html=False 转义成纯文本，不可能变成可执行的标签
    assert "<script" not in out
    assert "<img" not in out
    assert "&lt;script" in out


def test_render_blocks_javascript_links() -> None:
    md = "# 标题\n\n[点我](javascript:alert(1))\n"
    out = briefing.render_body_html(md)
    hrefs = re.findall(r'href="([^"]*)"', out)
    assert all(h.startswith(("http://", "https://")) for h in hrefs)


def test_render_keeps_http_links() -> None:
    out = briefing.render_body_html("# 标题\n\n[原文](https://example.com/a)\n")
    assert 'href="https://example.com/a"' in out


def test_render_document_escapes_title() -> None:
    out = briefing.render_document_html("# T\n", title="a<b>")
    assert out.startswith("<!DOCTYPE html>")
    assert "<title>a&lt;b&gt;</title>" in out


# ---------- 归档 ----------


def test_archive_writes_md_and_html(tmp_path: Path) -> None:
    md_path, html_path = briefing.archive_briefing(
        GOOD_MD, date="2026-10-14", title="每日 AI 简报（2026-10-14）", briefings_dir=tmp_path
    )
    assert md_path.name == "2026-10-14.md"
    assert html_path.name == "2026-10-14.html"
    assert md_path.read_text(encoding="utf-8") == GOOD_MD
    html = html_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html
    assert "example.com/news/1" in html
    assert not list(tmp_path.glob("*.tmp"))  # 原子写入不留临时文件


def test_archive_overwrites_existing(tmp_path: Path) -> None:
    briefing.archive_briefing(GOOD_MD, date="2026-10-14", title="t", briefings_dir=tmp_path)
    briefing.archive_briefing(EMPTY_MD, date="2026-10-14", title="t", briefings_dir=tmp_path)
    assert (tmp_path / "2026-10-14.md").read_text(encoding="utf-8") == EMPTY_MD


def test_archive_rejects_bad_date(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        briefing.archive_briefing(GOOD_MD, date="../evil", title="t", briefings_dir=tmp_path)
