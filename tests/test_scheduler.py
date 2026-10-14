"""进程内调度单测：时间解析、有效时间、任务参数、启动补跑、手动触发、随应用启停。

对应「调度控制」需求的验收：启动补跑当天、网页改时间即时重排、手动触发排队、
单实例不重叠、任务复用统一 run_daily()。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from news_agent.config import Config, LLMConfig, SMTPConfig
from news_agent.run_daily import RunDeps
from news_agent.storage import (
    SETTING_DAILY_RUN_AT,
    Database,
    get_setting,
    init_db,
    set_setting,
)
from news_agent.tools.registry import ToolRegistry
from news_agent.web import scheduler as scheduler_module
from news_agent.web.app import create_app
from news_agent.web.scheduler import (
    CATCH_UP_JOB_ID,
    JOB_ID,
    MANUAL_JOB_ID,
    build_scheduler,
    effective_daily_at,
    parse_at,
    reschedule_daily_run,
    schedule_catch_up,
    schedule_manual_run,
)

TZ = ZoneInfo("Asia/Shanghai")


def _deps(tmp_path: Path, *, daily_run_at: str = "07:00") -> RunDeps:
    briefings = tmp_path / "briefings"
    workspace = tmp_path / "workspace"
    briefings.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    db = Database(tmp_path / "sched.db", briefings_dir=briefings)
    init_db(db)
    config = Config(
        llm=LLMConfig(
            api_key="",
            base_url="http://unused.local",
            model="deepseek-flash",
            thinking_enabled=True,
            max_steps=30,
            token_budget=800_000,
            loop_window=3,
        ),
        tavily_api_key="",
        smtp=SMTPConfig(host="smtp.test", port=465, user="", password="", from_name="n"),
        daily_run_at=daily_run_at,
    )
    return RunDeps(
        config=config,
        db=db,
        client_factory=lambda cfg: None,
        sender=lambda *a, **k: None,
        registry=ToolRegistry(),
        briefings_dir=briefings,
        workspace_dir=workspace,
    )


def _job_ids(scheduler) -> set[str]:
    return {j.id for j in scheduler.get_jobs()}


# ---------- 时间解析与有效值 ----------


def test_parse_at_accepts_valid_values() -> None:
    assert parse_at("07:00") == (7, 0)
    assert parse_at("7:05") == (7, 5)
    assert parse_at(" 23:59 ") == (23, 59)


@pytest.mark.parametrize("bad", ["0700", "24:00", "07:60", "", "七点", None])
def test_parse_at_rejects_invalid_values(bad) -> None:
    with pytest.raises(ValueError, match="时间"):
        parse_at(bad)


def test_effective_daily_at_prefers_db_setting(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    assert effective_daily_at(deps.db, "07:00") == "07:00"
    set_setting(deps.db, SETTING_DAILY_RUN_AT, "08:15")
    assert effective_daily_at(deps.db, "07:00") == "08:15"


def test_effective_daily_at_falls_back_on_invalid(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    set_setting(deps.db, SETTING_DAILY_RUN_AT, "25:00")
    assert effective_daily_at(deps.db, "07:00") == "07:00"


# ---------- 任务注册 ----------


def test_build_scheduler_registers_daily_job(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    scheduler = build_scheduler(deps, at="07:00")

    jobs = scheduler.get_jobs()
    assert [j.id for j in jobs] == [JOB_ID]

    job = jobs[0]
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "7"
    assert fields["minute"] == "0"
    assert job.max_instances == 1  # 不重叠
    assert job.coalesce is True
    assert scheduler.running is False  # 由 lifespan 启动，构建时不启动


def test_reschedule_daily_run(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    scheduler = build_scheduler(deps, at="07:00")

    reschedule_daily_run(scheduler, at="09:30")

    fields = {f.name: str(f) for f in scheduler.get_job(JOB_ID).trigger.fields}
    assert fields["hour"] == "9"
    assert fields["minute"] == "30"


def test_job_calls_run_daily_with_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deps = _deps(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        scheduler_module, "run_daily", lambda *, deps: captured.setdefault("deps", deps)
    )

    scheduler = build_scheduler(deps, at="07:00")
    job = scheduler.get_job(JOB_ID)
    assert job is not None
    job.func()

    assert captured["deps"] is deps


# ---------- 启动补跑 ----------


def test_schedule_catch_up_skips_before_time(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    scheduler = build_scheduler(deps, at="07:00")

    scheduled = schedule_catch_up(
        scheduler, deps, at="07:00", now=datetime(2026, 10, 14, 6, 59, tzinfo=TZ)
    )

    assert scheduled is False
    assert _job_ids(scheduler) == {JOB_ID}


def test_schedule_catch_up_after_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "run_daily", lambda *, deps: None)
    deps = _deps(tmp_path)
    scheduler = build_scheduler(deps, at="07:00")

    scheduled = schedule_catch_up(
        scheduler, deps, at="07:00", now=datetime(2026, 10, 14, 7, 0, 5, tzinfo=TZ)
    )

    assert scheduled is True
    job = scheduler.get_job(CATCH_UP_JOB_ID)
    assert job is not None
    assert type(job.trigger).__name__ == "DateTrigger"
    assert job.max_instances == 1


def test_app_schedules_catch_up_when_time_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "run_daily", lambda *, deps: None)
    deps = _deps(tmp_path, daily_run_at="00:00")  # 任何时刻都已过：确定性
    app = create_app(db=deps.db, briefings_dir=deps.briefings_dir, run_deps=deps)

    assert CATCH_UP_JOB_ID in _job_ids(app.state.scheduler)


# ---------- 手动触发 ----------


def test_manual_run_queues_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "run_daily", lambda *, deps: None)
    deps = _deps(tmp_path)
    scheduler = build_scheduler(deps, at="07:00")

    schedule_manual_run(scheduler, deps)

    job = scheduler.get_job(MANUAL_JOB_ID)
    assert job is not None
    assert type(job.trigger).__name__ == "DateTrigger"


# ---------- 应用接线 ----------


def test_app_starts_and_stops_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "run_daily", lambda *, deps: None)
    deps = _deps(tmp_path)
    app = create_app(db=deps.db, briefings_dir=deps.briefings_dir, run_deps=deps)

    with TestClient(app):
        assert app.state.scheduler is not None
        assert app.state.scheduler.running is True
        assert JOB_ID in _job_ids(app.state.scheduler)

    assert app.state.scheduler.running is False  # 关闭即停止


def test_app_scheduler_disabled(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    app = create_app(db=deps.db, briefings_dir=deps.briefings_dir, enable_scheduler=False)

    with TestClient(app):
        assert app.state.scheduler is None
        assert app.state.run_deps is None


def test_app_run_now_and_schedule_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "run_daily", lambda *, deps: None)
    deps = _deps(tmp_path)
    app = create_app(db=deps.db, briefings_dir=deps.briefings_dir, run_deps=deps)

    with TestClient(app) as client:
        r = client.post("/run-now", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/briefings?triggered=1"
        assert app.state.scheduler.get_job(MANUAL_JOB_ID) is not None

        r = client.post("/schedule", data={"daily_at": "09:30"}, follow_redirects=False)
        assert r.status_code == 303
        fields = {f.name: str(f) for f in app.state.scheduler.get_job(JOB_ID).trigger.fields}
        assert fields["hour"] == "9" and fields["minute"] == "30"

    assert get_setting(deps.db, SETTING_DAILY_RUN_AT) == "09:30"
