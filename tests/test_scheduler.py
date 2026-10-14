"""进程内调度单测：时间解析、任务注册参数、任务调用 run_daily、随应用启停。

对应「调度改为 FastAPI + APScheduler」的验收：单实例不重叠、随 web 服务启停、
任务复用统一 run_daily()。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from news_agent.config import Config, LLMConfig, SMTPConfig
from news_agent.run_daily import RunDeps
from news_agent.storage import Database, init_db
from news_agent.tools.registry import ToolRegistry
from news_agent.web import scheduler as scheduler_module
from news_agent.web.app import create_app
from news_agent.web.scheduler import JOB_ID, build_scheduler, parse_at


def _deps(tmp_path: Path) -> RunDeps:
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
        daily_run_at="07:00",
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


def test_parse_at_accepts_valid_values() -> None:
    assert parse_at("07:00") == (7, 0)
    assert parse_at("7:05") == (7, 5)
    assert parse_at(" 23:59 ") == (23, 59)


@pytest.mark.parametrize("bad", ["0700", "24:00", "07:60", "", "七点", None])
def test_parse_at_rejects_invalid_values(bad) -> None:
    with pytest.raises(ValueError, match="DAILY_RUN_AT"):
        parse_at(bad)


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


def test_app_starts_and_stops_scheduler(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    app = create_app(db=deps.db, briefings_dir=deps.briefings_dir, run_deps=deps)

    with TestClient(app):
        assert app.state.scheduler is not None
        assert app.state.scheduler.running is True
        assert len(app.state.scheduler.get_jobs()) == 1

    assert app.state.scheduler.running is False  # 关闭即停止


def test_app_scheduler_disabled(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    app = create_app(
        db=deps.db, briefings_dir=deps.briefings_dir, enable_scheduler=False
    )

    with TestClient(app):
        assert app.state.scheduler is None
