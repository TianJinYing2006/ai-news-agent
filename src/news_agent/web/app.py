"""本机 Web 界面（docs §3.5）：身份 / 订阅 / 历史简报三页。

- 只监听 `127.0.0.1`（见 `__main__.py`），单机单用户，零登录；
- 服务端 Jinja2 渲染 + 普通表单 POST（PRG 模式），无构建链、无前端路由；
- 简报详情从归档的 `.md` 重新走 `render_body_html`（markdown-it 关闭原始 HTML +
  nh3 allowlist）后嵌入页面——正文只读文件不入库，页面上没有未净化的模型输出。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..briefing import render_body_html
from ..config import BRIEFINGS_DIR
from ..storage import (
    Database,
    get_briefing,
    get_profile,
    get_subscription,
    init_db,
    list_briefings,
    save_profile,
    save_subscription,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(*, db: Database | None = None, briefings_dir: Path | None = None) -> FastAPI:
    """构建应用。`db` / `briefings_dir` 可注入，便于 TestClient 使用临时目录。"""
    database = db or Database()
    briefings = Path(briefings_dir) if briefings_dir else BRIEFINGS_DIR
    init_db(database)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app = FastAPI(title="每日 AI 简报", docs_url=None, redoc_url=None, openapi_url=None)

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

    @app.get("/briefings", response_class=HTMLResponse)
    def briefings_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "briefings.html", {"briefings": list_briefings(database)}
        )

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
