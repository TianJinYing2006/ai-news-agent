"""Web 三页单测（FastAPI TestClient + 临时目录）。

验证：身份/订阅表单写库与回读、校验错误展示、历史列表状态与错误可见、
详情页渲染净化（模型输出不直接进页面）、未知日期 404。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from news_agent import storage
from news_agent.storage import Database, get_profile, get_subscription, init_db
from news_agent.web.app import create_app

DATE_SENT = "2026-10-13"
DATE_FAILED = "2026-10-14"

MD = """# 每日 AI 简报（2026-10-13）

## 某模型发布新版本

- 来源：量子位 · 2026-10-13
- 摘要：模型能力提升。
- 原文：https://example.com/news/1
"""


@pytest.fixture
def web(tmp_path: Path):
    briefings = tmp_path / "briefings"
    briefings.mkdir()
    db = Database(tmp_path / "web.db", briefings_dir=briefings)
    init_db(db)
    app = create_app(db=db, briefings_dir=briefings)
    yield TestClient(app), db, briefings
    db.close()


def _seed_sent(db: Database, briefings_dir: Path, date: str, md_text: str) -> None:
    storage.claim_daily_run(db, date)
    md_path = briefings_dir / f"{date}.md"
    html_path = briefings_dir / f"{date}.html"
    md_path.write_text(md_text, encoding="utf-8")
    html_path.write_text("<html></html>", encoding="utf-8")
    storage.mark_generated(
        db,
        date,
        md_path=str(md_path),
        html_path=str(html_path),
        summary="今日摘要",
        item_count=1,
    )
    storage.mark_sending(db, date)
    storage.mark_sent(db, date)


def _seed_failed(db: Database, date: str) -> None:
    storage.claim_daily_run(db, date)
    storage.mark_failed(db, date, "model", "达到步数上限 30")


# ---------- 身份 ----------


def test_profile_roundtrip(web) -> None:
    client, db, _ = web
    r = client.get("/profile")
    assert r.status_code == 200 and "身份设置" in r.text

    r = client.post(
        "/profile",
        data={"name": "张三", "email": "zhang@example.com"},
        follow_redirects=True,
    )
    assert r.status_code == 200 and "已保存" in r.text
    profile = get_profile(db)
    assert profile is not None and profile["email"] == "zhang@example.com"

    r = client.get("/profile")
    assert "zhang@example.com" in r.text


def test_profile_validation_error(web) -> None:
    client, db, _ = web
    r = client.post("/profile", data={"name": "张三", "email": "not-an-email"})
    assert r.status_code == 200 and "邮箱格式不正确" in r.text
    assert get_profile(db) is None


# ---------- 订阅 ----------


def test_subscription_roundtrip(web) -> None:
    client, db, _ = web
    r = client.post(
        "/subscription",
        data={
            "topics": "AI\n大模型，芯片",
            "keywords": "Agent",
            "exclude_keywords": "招聘",
            "max_items": "5",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200 and "已保存" in r.text

    sub = get_subscription(db)
    assert sub["topics"] == ["AI", "大模型", "芯片"]
    assert sub["keywords"] == ["Agent"]
    assert sub["exclude_keywords"] == ["招聘"]
    assert sub["max_items"] == 5

    r = client.get("/subscription")
    assert "大模型" in r.text


def test_subscription_bad_max_items(web) -> None:
    client, db, _ = web
    r = client.post(
        "/subscription",
        data={"topics": "", "keywords": "", "exclude_keywords": "", "max_items": "99"},
    )
    assert r.status_code == 200 and "max_items" in r.text
    assert get_subscription(db)["max_items"] == storage.DEFAULT_MAX_ITEMS


# ---------- 历史简报 ----------


def test_briefings_list_shows_status_and_error(web) -> None:
    client, db, briefings = web
    _seed_sent(db, briefings, DATE_SENT, MD)
    _seed_failed(db, DATE_FAILED)

    r = client.get("/briefings")
    assert r.status_code == 200
    assert DATE_SENT in r.text and DATE_FAILED in r.text
    assert "sent" in r.text and "failed" in r.text
    assert "今日摘要" in r.text
    assert "达到步数上限" in r.text


def test_detail_renders_sanitized_markdown(web) -> None:
    client, db, briefings = web
    _seed_sent(db, briefings, DATE_SENT, MD + "\n<script>alert(1)</script>\n")

    r = client.get(f"/briefings/{DATE_SENT}")
    assert r.status_code == 200
    assert "某模型发布新版本" in r.text
    assert "example.com/news/1" in r.text
    assert "<script" not in r.text  # 模型内容经过净化，不会以标签形式进页面


def test_detail_failed_shows_error_without_body(web) -> None:
    client, db, _ = web
    _seed_failed(db, DATE_FAILED)

    r = client.get(f"/briefings/{DATE_FAILED}")
    assert r.status_code == 200
    assert "达到步数上限" in r.text
    assert "没有可查看的简报正文" in r.text


def test_detail_unknown_or_invalid_date_404(web) -> None:
    client, _, _ = web
    assert client.get("/briefings/1999-01-01").status_code == 404
    assert client.get("/briefings/not-a-date").status_code == 404


def test_home_redirects_to_briefings(web) -> None:
    client, _, _ = web
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/briefings"
