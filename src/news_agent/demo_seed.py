"""演示数据种子：无凭据也能体验完整的"历史简报"浏览（开箱即用）。

- 只写**过去日期**（不碰当天调度与正式流转）；
- 所有内容带「演示数据」标记；`--clean` 只清理带标记的记录与文件；
- 幂等：已有记录的日期直接跳过，可重复执行；
- 演示模板会先经过真实的简报校验（标题/条目/URL），格式与正式产物一致。

用法：

    python -m news_agent.demo_seed          # 写入最近 2 天的演示简报
    python -m news_agent.demo_seed --clean  # 清除演示数据
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .briefing import archive_briefing, check_briefing_text
from .config import BRIEFINGS_DIR
from .storage import (
    Database,
    claim_daily_run,
    get_briefing,
    init_db,
    mark_generated,
    mark_sending,
    mark_sent,
)

DEMO_MARKER = "演示数据"
TZ = ZoneInfo("Asia/Shanghai")

_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    (
        "某头部实验室发布新一代多模态模型",
        "官方博客",
        (
            "新模型在推理与工具调用基准上刷新纪录，并开放了更长的上下文窗口。"
            "本条目为演示数据，仅用于展示简报版式。"
        ),
        "https://example.com/demo/model-release",
    ),
    (
        "开源社区推出轻量级 Agent 编排框架",
        "开源周报",
        (
            "框架主打最小依赖与可观测性，官方示例覆盖新闻聚合、代码审查等场景。"
            "本条目为演示数据。"
        ),
        "https://example.com/demo/open-source-agent",
    ),
    (
        "监管机构就生成式 AI 内容标注发布征求意见稿",
        "行业观察",
        "草案要求平台对合成内容提供显著标识；本条目为演示数据，不代表真实事件。",
        "https://example.com/demo/regulation",
    ),
)


def _demo_markdown(day: str) -> str:
    lines = [f"# 每日 AI 简报（{day}）· {DEMO_MARKER}", ""]
    for title, source, summary, url in _ITEMS:
        lines += [
            f"## {title}",
            "",
            f"- 来源：{source} · {day}",
            f"- 摘要：{summary}",
            f"- 原文：{url}",
            "",
        ]
    lines += [f"> 以上内容为{DEMO_MARKER}，用于展示历史简报页面；可用 `python -m news_agent.demo_seed --clean` 清除。", ""]
    return "\n".join(lines)


def seed(
    db: Database,
    briefings_dir: Path,
    *,
    days: int = 2,
    today: str | None = None,
) -> list[str]:
    """写入最近 `days` 天的演示简报（跳过已有记录的日期）。返回写入的日期列表。"""
    base = date.fromisoformat(today) if today else datetime.now(TZ).date()
    seeded: list[str] = []
    for offset in range(1, days + 1):
        day = (base - timedelta(days=offset)).isoformat()
        if get_briefing(db, day) is not None:
            continue
        if claim_daily_run(db, day) != "generate":  # 防御：非预期状态不硬写
            continue

        md_text = _demo_markdown(day)
        check = check_briefing_text(md_text, max_items=50)
        if not check.ok:
            raise RuntimeError(f"演示模板未通过简报校验：{check.error}")

        md_path, html_path = archive_briefing(
            md_text,
            date=day,
            title=f"每日 AI 简报（{day}）· {DEMO_MARKER}",
            briefings_dir=briefings_dir,
        )
        mark_generated(
            db,
            day,
            md_path=str(md_path),
            html_path=str(html_path),
            summary=f"（{DEMO_MARKER}）示例简报：{len(_ITEMS)} 条",
            item_count=len(_ITEMS),
        )
        mark_sending(db, day)
        mark_sent(db, day)
        seeded.append(day)
    return seeded


def clean(db: Database, briefings_dir: Path) -> list[str]:
    """删除带演示标记的记录与归档文件（只动演示数据）。返回清理的日期列表。"""
    rows = db.fetchall("SELECT date FROM briefings WHERE summary LIKE ?", (f"%{DEMO_MARKER}%",))
    removed: list[str] = []
    for row in rows:
        day = row["date"]
        for suffix in (".md", ".html"):
            path = Path(str(briefings_dir)) / f"{day}{suffix}"
            if path.is_file():
                path.unlink()
        db.execute("DELETE FROM briefings WHERE date = ?", (day,))
        removed.append(day)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="news_agent.demo_seed", description="演示数据种子（写入/清理，标注为演示数据）"
    )
    parser.add_argument("--clean", action="store_true", help="清除演示数据")
    parser.add_argument("--days", type=int, default=2, help="写入最近几天的演示简报（默认 2）")
    args = parser.parse_args(argv)

    db = Database()
    init_db(db)
    try:
        if args.clean:
            removed = clean(db, BRIEFINGS_DIR)
            detail = f"（{', '.join(removed)}）" if removed else ""
            print(f"已清除演示数据：{len(removed)} 天{detail}")
        else:
            seeded = seed(db, BRIEFINGS_DIR, days=max(1, min(args.days, 7)))
            if seeded:
                print(
                    f"已写入演示数据：{', '.join(seeded)}"
                    f"（标记为「{DEMO_MARKER}」，可用 --clean 清除）"
                )
            else:
                print("没有需要写入的日期（均已存在记录）。")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
