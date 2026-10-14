"""存储层单测：schema、偏好 CRUD、状态机、原子认领与陈旧租约回收。

对应实施计划 §3.1/§3.2 的验收点：
- 同日两个并发 claim 只有一个能生成（BEGIN IMMEDIATE 串行化）；
- sent 跳过、租约内 already_running、过期回收；
- SMTP 失败但产物可用 → retry_send（不重跑 LLM）；产物缺失/不合格 → generate。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from news_agent.storage import db as storage

DATE = "2026-10-14"
TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def db(tmp_path: Path) -> storage.Database:
    briefings = tmp_path / "briefings"
    briefings.mkdir()
    database = storage.Database(tmp_path / "test.db", briefings_dir=briefings)
    storage.init_db(database)
    yield database
    database.close()


def _backdate(db: storage.Database, minutes: int) -> None:
    old = datetime.now(TZ) - timedelta(minutes=minutes)
    db.execute(
        "UPDATE briefings SET started_at = ? WHERE date = ?",
        (old.isoformat(timespec="seconds"), DATE),
    )


def _write_artifacts(db: storage.Database, md: str = "# 简报\n\n- 内容", html: str = "<h1>简</h1>") -> None:
    (db.briefings_dir / f"{DATE}.md").write_text(md, encoding="utf-8")
    (db.briefings_dir / f"{DATE}.html").write_text(html, encoding="utf-8")


def _generate(db: storage.Database) -> None:
    """走完 generating → generated，并写入可用产物。"""
    _write_artifacts(db)
    storage.mark_generated(
        db,
        DATE,
        md_path=str(db.briefings_dir / f"{DATE}.md"),
        html_path=str(db.briefings_dir / f"{DATE}.html"),
        summary="今日简报",
        item_count=3,
    )


def _briefing(db: storage.Database) -> dict:
    row = db.fetchone("SELECT * FROM briefings WHERE date = ?", (DATE,))
    assert row is not None
    return row


# ---------- schema ----------


def test_init_db_idempotent(db: storage.Database) -> None:
    storage.init_db(db)  # 再跑一次不能报错
    tables = {r["name"] for r in db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"profile", "subscription", "briefings", "agent_runs"} <= tables


def test_briefings_row_created_with_expected_paths(db: storage.Database) -> None:
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_GENERATE
    row = _briefing(db)
    assert row["status"] == "generating"
    assert row["attempt"] == 1
    assert row["md_path"] == str(db.briefings_dir / f"{DATE}.md")
    assert row["html_path"] == str(db.briefings_dir / f"{DATE}.html")
    assert row["started_at"]


# ---------- 身份 / 订阅 ----------


def test_profile_roundtrip_and_upsert(db: storage.Database) -> None:
    assert storage.get_profile(db) is None
    storage.save_profile(db, "张三", "zhang@example.com")
    assert storage.get_profile(db)["email"] == "zhang@example.com"

    storage.save_profile(db, "李四", "li@example.com")
    profile = storage.get_profile(db)
    assert profile["name"] == "李四"
    assert profile["updated_at"]


def test_profile_rejects_bad_input(db: storage.Database) -> None:
    with pytest.raises(ValueError):
        storage.save_profile(db, "", "a@b.com")
    with pytest.raises(ValueError):
        storage.save_profile(db, "张三", "not-an-email")
    with pytest.raises(ValueError):
        storage.save_profile(db, "长" * 200, "a@b.com")


def test_subscription_defaults_when_unset(db: storage.Database) -> None:
    sub = storage.get_subscription(db)
    assert sub == {
        "topics": [],
        "keywords": [],
        "exclude_keywords": [],
        "max_items": storage.DEFAULT_MAX_ITEMS,
        "updated_at": None,
    }


def test_subscription_roundtrip_trims_and_dedups(db: storage.Database) -> None:
    storage.save_subscription(
        db,
        topics=[" AI ", "", "AI", "芯片"],
        keywords=["GPT"],
        exclude_keywords=["招聘"],
        max_items=8,
    )
    sub = storage.get_subscription(db)
    assert sub["topics"] == ["AI", "芯片"]
    assert sub["keywords"] == ["GPT"]
    assert sub["exclude_keywords"] == ["招聘"]
    assert sub["max_items"] == 8


def test_subscription_rejects_bad_input(db: storage.Database) -> None:
    with pytest.raises(TypeError):
        storage.save_subscription(db, "AI", [], [], 10)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        storage.save_subscription(db, ["AI", 3], [], [], 10)  # type: ignore[list-item]
    with pytest.raises(ValueError):
        storage.save_subscription(db, ["x" * 200], [], [], 10)
    with pytest.raises(ValueError):
        storage.save_subscription(db, [f"x{i}" for i in range(101)], [], [], 10)
    for bad_max in (0, 51):
        with pytest.raises(ValueError):
            storage.save_subscription(db, [], [], [], bad_max)
    for bad_type in (True, "10"):
        with pytest.raises(TypeError):
            storage.save_subscription(db, [], [], [], bad_type)  # type: ignore[arg-type]


# ---------- 运行设置 ----------


def test_settings_roundtrip_and_upsert(db: storage.Database) -> None:
    assert storage.get_setting(db, storage.SETTING_DAILY_RUN_AT) is None
    storage.set_setting(db, storage.SETTING_DAILY_RUN_AT, "08:30")
    assert storage.get_setting(db, storage.SETTING_DAILY_RUN_AT) == "08:30"
    storage.set_setting(db, storage.SETTING_DAILY_RUN_AT, "09:00")
    assert storage.get_setting(db, storage.SETTING_DAILY_RUN_AT) == "09:00"


def test_settings_validation(db: storage.Database) -> None:
    with pytest.raises(ValueError):
        storage.set_setting(db, "", "x")
    with pytest.raises(ValueError):
        storage.set_setting(db, "k", "   ")
    with pytest.raises(ValueError):
        storage.set_setting(db, "k", "x" * 500)


# ---------- 原子认领 ----------


def test_claim_within_lease_is_already_running(db: storage.Database) -> None:
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_GENERATE
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_ALREADY_RUNNING
    assert _briefing(db)["attempt"] == 1


def test_claim_sent_is_skipped(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    _generate(db)
    storage.mark_sending(db, DATE)
    storage.mark_sent(db, DATE)
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_SKIPPED
    assert _briefing(db)["sent_at"]


def test_claim_after_model_failure_regenerates(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    storage.mark_failed(db, DATE, "model", "达到步数上限")
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_GENERATE
    row = _briefing(db)
    assert row["attempt"] == 2
    assert row["failure_stage"] is None and row["error"] is None
    assert row["summary"] is None


def test_claim_stale_generating_regenerates(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    _backdate(db, storage.STALE_AFTER_MINUTES + 1)
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_GENERATE
    assert _briefing(db)["attempt"] == 2


def test_claim_stale_sending_with_artifacts_retries_send(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    _generate(db)
    storage.mark_sending(db, DATE)
    _backdate(db, storage.STALE_AFTER_MINUTES + 1)
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_RETRY_SEND
    row = _briefing(db)
    assert row["status"] == "sending"
    assert row["summary"] == "今日简报"  # 复用产物，摘要保留
    assert row["attempt"] == 2


def test_claim_stale_sending_without_files_regenerates(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    _generate(db)
    storage.mark_sending(db, DATE)
    (db.briefings_dir / f"{DATE}.md").unlink()
    _backdate(db, storage.STALE_AFTER_MINUTES + 1)
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_GENERATE


def test_claim_smtp_failure_with_artifacts_retries_send(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    _generate(db)
    storage.mark_sending(db, DATE)
    storage.mark_failed(db, DATE, storage.FAILURE_STAGE_SMTP, "SMTP 连接超时")
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_RETRY_SEND
    row = _briefing(db)
    assert row["status"] == "sending"
    assert row["summary"] == "今日简报"
    assert row["attempt"] == 2


def test_claim_smtp_failure_without_files_regenerates(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    _generate(db)
    storage.mark_sending(db, DATE)
    storage.mark_failed(db, DATE, storage.FAILURE_STAGE_SMTP, "SMTP 连接超时")
    (db.briefings_dir / f"{DATE}.html").unlink()
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_GENERATE


def test_claim_smtp_failure_with_empty_artifact_regenerates(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    _generate(db)
    (db.briefings_dir / f"{DATE}.md").write_text("", encoding="utf-8")
    storage.mark_failed(db, DATE, storage.FAILURE_STAGE_SMTP, "SMTP 连接超时")
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_GENERATE


def test_claim_artifact_outside_briefings_dir_not_usable(db: storage.Database, tmp_path: Path) -> None:
    """产物路径越出简报目录时视为不可用：宁可重生成，不拿可疑文件发信。"""
    storage.claim_daily_run(db, DATE)
    _generate(db)
    outside = tmp_path / "evil.md"
    outside.write_text("x", encoding="utf-8")
    db.execute(
        "UPDATE briefings SET md_path = ?, status = 'failed', failure_stage = ? WHERE date = ?",
        (str(outside), storage.FAILURE_STAGE_SMTP, DATE),
    )
    assert storage.claim_daily_run(db, DATE) == storage.CLAIM_GENERATE


def test_claim_rejects_invalid_date(db: storage.Database) -> None:
    for bad in ("2026/10/14", "../x", "2026-10-32", "20261014"):
        with pytest.raises(ValueError):
            storage.claim_daily_run(db, bad)


def test_concurrent_claims_only_one_generates(tmp_path: Path) -> None:
    briefings = tmp_path / "briefings"
    briefings.mkdir()
    db_path = tmp_path / "concurrent.db"
    setup = storage.Database(db_path, briefings_dir=briefings)
    storage.init_db(setup)
    setup.close()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker() -> None:
        database = storage.Database(db_path, briefings_dir=briefings)
        try:
            barrier.wait(timeout=10)
            results.append(storage.claim_daily_run(database, DATE))
        finally:
            database.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert sorted(results) == [storage.CLAIM_ALREADY_RUNNING, storage.CLAIM_GENERATE]

    check = storage.Database(db_path, briefings_dir=briefings)
    try:
        row = check.fetchone("SELECT attempt FROM briefings WHERE date = ?", (DATE,))
        assert row is not None and row["attempt"] == 1
    finally:
        check.close()


# ---------- 状态转换 ----------


def test_mark_generated_requires_generating_state(db: storage.Database) -> None:
    with pytest.raises(RuntimeError):
        _generate(db)  # 还没 claim，行不存在
    storage.claim_daily_run(db, DATE)
    _generate(db)
    with pytest.raises(RuntimeError):
        _generate(db)  # 已经是 generated，不允许二次落库


def test_mark_generated_validates_payload(db: storage.Database, tmp_path: Path) -> None:
    storage.claim_daily_run(db, DATE)
    good_md = str(db.briefings_dir / f"{DATE}.md")
    good_html = str(db.briefings_dir / f"{DATE}.html")
    with pytest.raises(ValueError):
        storage.mark_generated(
            db, DATE, md_path=str(tmp_path / "evil.md"), html_path=good_html,
            summary="摘要", item_count=1,
        )
    with pytest.raises(ValueError):
        storage.mark_generated(
            db, DATE, md_path=good_md, html_path=good_html, summary=" ", item_count=1,
        )
    with pytest.raises(ValueError):
        storage.mark_generated(
            db, DATE, md_path=good_md, html_path=good_html, summary="摘要", item_count=-1,
        )


def test_mark_sending_requires_generated(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    with pytest.raises(RuntimeError):
        storage.mark_sending(db, DATE)  # generating 不能直接进入 sending


def test_sent_is_terminal(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    _generate(db)
    storage.mark_sending(db, DATE)
    storage.mark_sent(db, DATE)
    with pytest.raises(RuntimeError):
        storage.mark_failed(db, DATE, "smtp", "不该发生")
    assert _briefing(db)["status"] == "sent"


def test_mark_failed_stores_stage_and_truncates_error(db: storage.Database) -> None:
    storage.claim_daily_run(db, DATE)
    storage.mark_failed(db, DATE, "validation", "x" * 5000)
    row = _briefing(db)
    assert row["status"] == "failed"
    assert row["failure_stage"] == "validation"
    assert len(row["error"]) == 2000


# ---------- agent_runs ----------


def test_insert_run_stores_stats_and_trace(db: storage.Database) -> None:
    run_id = storage.insert_run(
        db,
        DATE,
        "2026-10-14T07:00:00+08:00",
        "2026-10-14T07:02:00+08:00",
        "finished",
        [{"tool": "fetch_rss", "summary": "抓取源 A"}],
        None,
        steps=5,
        tool_calls=7,
        tokens_in=1200,
        tokens_out=800,
    )
    assert run_id > 0
    row = db.fetchone("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
    assert row is not None
    assert row["steps"] == 5 and row["tool_calls"] == 7
    assert row["tokens_in"] == 1200 and row["tokens_out"] == 800
    assert json.loads(row["trace_json"])[0]["summary"] == "抓取源 A"
