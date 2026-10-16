"""本机 Web 界面（docs §3.5）：身份 / 订阅 / 历史简报三页。

- 只监听 `127.0.0.1`（见 `__main__.py`），单机单用户，零登录；
- 服务端 Jinja2 渲染 + 普通表单 POST（PRG 模式），无构建链、无前端路由；
- 简报详情从归档的 `.md` 重新走 `render_body_html`（markdown-it 关闭原始 HTML +
  nh3 allowlist）后嵌入页面——正文只读文件不入库，页面上没有未净化的模型输出；
- 启动时注册进程内调度（APScheduler，见 `scheduler.py`），关闭即停止；启动时按需补跑当天；
  历史页提供「推送时间设置」（存 SQLite，保存即重排）与「立即运行一次」（后台排队，防重不变）；
  `enable_scheduler=False` 供测试关闭。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..briefing import render_body_html
from ..config import BRIEFINGS_DIR
from ..run_daily import RunDeps
from ..storage import (
    SETTING_DAILY_RUN_AT,
    Database,
    get_briefing,
    get_profile,
    get_subscription,
    init_db,
    list_briefings,
    save_profile,
    save_subscription,
    set_setting,
)
from .scheduler import (
    build_scheduler,
    effective_daily_at,
    parse_at,
    reschedule_daily_run,
    schedule_catch_up,
    schedule_manual_run,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"

_TRIGGER_MESSAGES = {
    "normal": "已触发，正在后台生成；稍后刷新本页查看状态。",
    "force": "已触发「强制重跑」：将重新生成简报并再次发送邮件（约 1–3 分钟），完成后刷新查看。",
    "resend": "已触发「重发邮件」：仅当存在已归档简报时执行，稍后刷新查看。",
}


def create_app(
    *,
    db: Database | None = None,
    briefings_dir: Path | None = None,
    run_deps: RunDeps | None = None,
    enable_scheduler: bool = True,
) -> FastAPI:
    """构建应用。`db` / `briefings_dir` / `run_deps` 可注入；测试可关调度器。"""
    database = db or Database()
    briefings = Path(briefings_dir) if briefings_dir else BRIEFINGS_DIR
    init_db(database)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    deps = run_deps

    def _effective_at() -> str:
        default = deps.config.daily_run_at if deps is not None else "07:00"
        return effective_daily_at(database, default)

    scheduler: BackgroundScheduler | None = None
    if enable_scheduler:
        deps = deps or RunDeps.default()
        at = _effective_at()
        scheduler = build_scheduler(deps, at=at)
        schedule_catch_up(scheduler, deps, at=at)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if scheduler is not None:
            scheduler.start()
        yield
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)

    app = FastAPI(
        title="每日 AI 简报", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.scheduler = scheduler
    app.state.run_deps = deps

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/briefings", status_code=307)

    # ---------- 身份 ----------

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request, saved: int = 0) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "profile.html",
            {"profile": get_profile(database), "saved": saved, "error": None},
        )

    @app.post("/profile", response_class=HTMLResponse)
    def profile_save(
        request: Request,
        name: str = Form(""),
        email: str = Form(""),
    ) -> HTMLResponse:
        try:
            save_profile(database, name, email)
        except (TypeError, ValueError) as e:
            return templates.TemplateResponse(
                request,
                "profile.html",
                {"profile": get_profile(database), "saved": 0, "error": str(e)},
            )
        return RedirectResponse("/profile?saved=1", status_code=303)

    # ---------- 订阅 ----------

    @app.get("/subscription", response_class=HTMLResponse)
    def subscription_page(request: Request, saved: int = 0) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "subscription.html",
            {"subscription": get_subscription(database), "saved": saved, "error": None},
        )

    @app.post("/subscription", response_class=HTMLResponse)
    def subscription_save(
        request: Request,
        topics: str = Form(""),
        keywords: str = Form(""),
        exclude_keywords: str = Form(""),
        max_items: str = Form("10"),
    ) -> HTMLResponse:
        try:
            save_subscription(
                database,
                _split_lines(topics),
                _split_lines(keywords),
                _split_lines(exclude_keywords),
                int(max_items),
            )
        except (TypeError, ValueError) as e:
            return templates.TemplateResponse(
                request,
                "subscription.html",
                {"subscription": get_subscription(database), "saved": 0, "error": str(e)},
            )
        return RedirectResponse("/subscription?saved=1", status_code=303)

    # ---------- 历史简报 ----------

    def _render_briefings(
        request: Request,
        *,
        saved: int = 0,
        triggered: str = "",
        error: str | None = None,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "briefings.html",
            {
                "briefings": list_briefings(database),
                "daily_at": _effective_at(),
                "saved": saved,
                "triggered_message": _TRIGGER_MESSAGES.get(triggered, "") if triggered else "",
                "error": error,
            },
        )

    @app.get("/briefings", response_class=HTMLResponse)
    def briefings_page(request: Request, saved: int = 0, triggered: str = "") -> HTMLResponse:
        return _render_briefings(request, saved=saved, triggered=triggered)

    @app.post("/schedule", response_class=HTMLResponse)
    def save_schedule(request: Request, daily_at: str = Form("")) -> HTMLResponse:
        try:
            parse_at(daily_at)
        except ValueError as e:
            return _render_briefings(request, error=str(e))
        set_setting(database, SETTING_DAILY_RUN_AT, daily_at.strip())
        if scheduler is not None:
            reschedule_daily_run(scheduler, at=daily_at.strip())
        return RedirectResponse("/briefings?saved=1", status_code=303)

    @app.post("/run-now", response_class=HTMLResponse)
    def run_now(request: Request, mode: str = Form("normal")) -> HTMLResponse:
        if scheduler is None or deps is None:
            return _render_briefings(request, error="调度器未启用，无法从网页触发运行")
        if mode not in _TRIGGER_MESSAGES:
            mode = "normal"
        schedule_manual_run(scheduler, deps, mode=mode)
        return RedirectResponse(f"/briefings?triggered={mode}", status_code=303)

    @app.get("/briefings/{date}", response_class=HTMLResponse)
    def briefing_detail(request: Request, date: str) -> HTMLResponse:
        try:
            row = get_briefing(database, date)
        except ValueError:
            row = None
        if row is None:
            return HTMLResponse("<h1>404：没有这一天的记录</h1>", status_code=404)

        body_html = ""
        md_path = briefings / f"{date}.md"
        if md_path.is_file():
            body_html = render_body_html(md_path.read_text(encoding="utf-8", errors="replace"))
        return templates.TemplateResponse(
            request,
            "briefing_detail.html",
            {"row": row, "date": date, "body_html": body_html},
        )

    return app


def _split_lines(raw: str) -> list[str]:
    """一行（或逗号/分号分隔）一项，去掉空项。"""
    text = re.sub(r"[，,;；]", "\n", raw or "")
    return [line.strip() for line in text.splitlines() if line.strip()]
