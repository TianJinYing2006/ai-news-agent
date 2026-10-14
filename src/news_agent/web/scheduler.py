"""进程内调度：APScheduler（随 FastAPI 启停，决策见 docs 实施计划 §1/§4）。

- 启动 web 服务即注册每日任务：`DAILY_RUN_AT`（默认 07:00，Asia/Shanghai）调用 `run_daily()`；
- 单进程单 worker、任务不重叠（`max_instances=1`、`coalesce=True`）、`MemoryJobStore`
  —— 不引入持久化任务库，发送时间只作为配置项，不做网页动态修改；
- **服务运行期间才会触发**：关闭 / 休眠期间错过的任务不自动补跑，手动跑一次 CLI 即可（README 说明）；
- SQLite 原子认领是第二道防重：即使误启多进程，同日也只有一个进程能真正生成与发送。
"""

from __future__ import annotations

import re

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ..run_daily import RunDeps, run_daily

JOB_ID = "daily_run"
TIMEZONE = "Asia/Shanghai"


def parse_at(value: str) -> tuple[int, int]:
    """解析 `HH:MM`；格式错误给出可读异常（启动时即暴露配置问题）。"""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", (value or "").strip())
    if match is None:
        raise ValueError(f"DAILY_RUN_AT 需要 HH:MM 格式，收到：{value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"DAILY_RUN_AT 超出范围：{value!r}")
    return hour, minute


def build_scheduler(
    deps: RunDeps,
    *,
    at: str,
    timezone: str = TIMEZONE,
) -> BackgroundScheduler:
    """构建调度器（未启动；由 FastAPI lifespan 启动与关闭）。"""

    def job() -> None:
        run_daily(deps=deps)

    hour, minute = parse_at(at)
    scheduler = BackgroundScheduler(timezone=timezone)
    scheduler.add_job(
        job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
