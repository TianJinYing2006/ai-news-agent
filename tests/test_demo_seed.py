"""演示数据种子单测：写入/标记/幂等/跳过已有记录/只清理演示数据。"""

from __future__ import annotations

from pathlib import Path

import pytest

from news_agent import demo_seed, storage
from news_agent.storage import Database, get_briefing, init_db


@pytest.fixture
def env(tmp_path: Path):
    briefings = tmp_path / "briefings"
    briefings.mkdir()
    db = Database(tmp_path / "demo.db", briefings_dir=briefings)
    init_db(db)
    yield db, briefings
    db.close()


def test_seed_writes_marked_demo_briefings(env) -> None:
    db, briefings = env
    seeded = demo_seed.seed(db, briefings, days=2, today="2026-10-14")

    assert seeded == ["2026-10-13", "2026-10-12"]
    for day in seeded:
        row = get_briefing(db, day)
        assert row is not None and row["status"] == "sent"
        assert row["summary"] and demo_seed.DEMO_MARKER in row["summary"]
        assert (briefings / f"{day}.md").is_file()
        assert (briefings / f"{day}.html").is_file()

    text = (briefings / "2026-10-13.md").read_text(encoding="utf-8")
    assert "## " in text and "https://example.com/demo/" in text


def test_seed_is_idempotent(env) -> None:
    db, briefings = env
    first = demo_seed.seed(db, briefings, days=2, today="2026-10-14")
    second = demo_seed.seed(db, briefings, days=2, today="2026-10-14")

    assert first == ["2026-10-13", "2026-10-12"]
    assert second == []


def test_seed_skips_dates_with_existing_records(env) -> None:
    db, briefings = env
    storage.claim_daily_run(db, "2026-10-13")
    storage.mark_failed(db, "2026-10-13", "model", "真实记录占位")

    seeded = demo_seed.seed(db, briefings, days=2, today="2026-10-14")

    assert seeded == ["2026-10-12"]  # 已有记录的日期不覆盖
    row = get_briefing(db, "2026-10-13")
    assert row is not None and row["status"] == "failed"
    assert not (row["summary"] or "").count(demo_seed.DEMO_MARKER)


def test_clean_removes_only_demo_data(env) -> None:
    db, briefings = env
    demo_seed.seed(db, briefings, days=1, today="2026-10-14")  # 10-13

    storage.claim_daily_run(db, "2026-10-12")
    storage.mark_failed(db, "2026-10-12", "model", "真实失败记录")
    (briefings / "2026-10-12.md").write_text("# 真实简报\n", encoding="utf-8")

    removed = demo_seed.clean(db, briefings)

    assert removed == ["2026-10-13"]
    assert get_briefing(db, "2026-10-13") is None
    assert not (briefings / "2026-10-13.md").exists()
    assert not (briefings / "2026-10-13.html").exists()
    assert get_briefing(db, "2026-10-12") is not None  # 非演示数据保留
    assert (briefings / "2026-10-12.md").is_file()
