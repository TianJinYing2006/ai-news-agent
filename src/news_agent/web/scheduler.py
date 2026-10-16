"""进程内调度：APScheduler（随 FastAPI 启停，决策见 docs 实施计划 §1/§4）。

- 每日任务：推送时间默认取 `.env` `DAILY_RUN_AT`，**网页可修改**（存 SQLite `settings`，
  优先于环境默认值），保存后即时重排，无需重启；
- **启动补跑**：服务启动时若当天已过推送时间，立即补跑一次。是否真正执行交给
  `run_daily` 的状态机与原子认领（已 sent → 跳过；失败 → 按其阶段重试）；历史日期不补发；
- **手动触发**：网页「立即运行一次」把 `run_daily` 作为一次性任务排队（不阻塞请求线程），
  防重同样由原子认领保证；
- 单进程单 worker、任务不重叠（`max_instances=1`、`coalesce=True`）、`MemoryJobStore`
  —— 不引入持久化任务库；
- SQLite 原子认领是第二道防重：即使误启多进程，同日也只有一个进程能真正生成与发送。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..run_daily import RunDeps, run_daily
from ..storage import SETTING_DAILY_RUN_AT, Database, get_setting

JOB_ID = "daily_run"
CATCH_UP_JOB_ID = "startup_catch_up"
MANUAL_JOB_ID = "manual_run"
TIMEZONE = "Asia/Shanghai"


def parse_at(value: str) -> tuple[int, int]:
    """解析 `HH:MM`；格式错误给出可读异常（启动/保存时即暴露配置问题）。"""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", (value or "").strip())
    if match is None:
        raise ValueError(f"时间需要 HH:MM 格式，收到：{value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围：{value!r}")
    return hour, minute


def effective_daily_at(db: Database, default: str) -> str:
    """推送时间的生效值：网页设置（SQLite）优先；非法/缺失回退默认值。"""
    value = (get_setting(db, SETTING_DAILY_RUN_AT) or "").strip() or default
    try:
        parse_at(value)
    except ValueError:
        return default
    return value


def cron_trigger(at: str, timezone: str = TIMEZONE) -> CronTrigger:
    hour, minute = parse_at(at)
    return CronTrigger(hour=hour, minute=minute, timezone=timezone)


def _make_job(deps: RunDeps, **run_kwargs: object) -> Callable[[], None]:
    def job() -> None:
        run_daily(deps=deps, **run_kwargs)

    return job


def build_scheduler(deps: RunDeps, *, at: str, timezone: str = TIMEZONE) -> BackgroundScheduler:
    """构建调度器（未启动；由 FastAPI lifespan 启动与关闭）。"""
    scheduler = BackgroundScheduler(timezone=timezone)
    scheduler.add_job(
        _make_job(deps),
        trigger=cron_trigger(at, timezone),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def reschedule_daily_run(
    scheduler: BackgroundScheduler,
    *,
    at: str,
    timezone: str = TIMEZONE,
) -> None:
    """网页改推送时间后即时重排（不需要重启服务）。"""
    scheduler.reschedule_job(JOB_ID, trigger=cron_trigger(at, timezone))


def schedule_catch_up(
    scheduler: BackgroundScheduler,
    deps: RunDeps,
    *,
    at: str,
    now: datetime | None = None,
) -> bool:
    """当天已过推送时间时，安排一次立即补跑；返回是否已安排。

    是否真正执行交给 `run_daily`：已 sent 会被原子认领跳过，失败则按其阶段重试/重生成。
    """
    current = now or datetime.now(ZoneInfo(TIMEZONE))
    hour, minute = parse_at(at)
    if (current.hour, current.minute) < (hour, minute):
        return False
    scheduler.add_job(
        _make_job(deps),
        trigger="date",
        run_date=current + timedelta(seconds=2),
        id=CATCH_UP_JOB_ID,
        replace_existing=True,
        max_instances=1,
    )
    return True


def schedule_manual_run(
    scheduler: BackgroundScheduler,
    deps: RunDeps,
    *,
    mode: str = "normal",
) -> None:
    """网页手动触发：一次性任务排队，不阻塞请求线程。

    `mode`：`normal`（普通防重）/ `force`（强制重跑）/ `resend`（仅重发）；
    非法值按 `normal` 处理，越权语义只在 storage 的原子认领里生效。
    """
    kwargs: dict[str, object] = {}
    if mode == "force":
        kwargs["force"] = True
    elif mode == "resend":
        kwargs["resend"] = True

    scheduler.add_job(
        _make_job(deps, **kwargs),
        trigger="date",
        run_date=datetime.now(ZoneInfo(TIMEZONE)) + timedelta(seconds=2),
        id=MANUAL_JOB_ID,
        replace_existing=True,
        max_instances=1,
    )
