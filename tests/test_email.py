"""SMTP 发送单测（实施计划阶段 4 的发送部分）。

全部 mock smtplib，不联网：成功、失败重试后成功、重试耗尽、
凭据/收件人缺失不发起连接、SSL 与 STARTTLS 分支、消息结构（text+html）。
"""

from __future__ import annotations

import smtplib
from typing import Any, Self

import pytest

from news_agent.config import SMTPConfig
from news_agent.delivery import email as mailer


def _cfg(**overrides: Any) -> SMTPConfig:
    values: dict[str, Any] = {
        "host": "smtp.test",
        "port": 465,
        "user": "sender@test.com",
        "password": "auth-code",
        "from_name": "News Agent",
        "retries": 2,
        "retry_delay": 5.0,
    }
    values.update(overrides)
    return SMTPConfig(**values)


def _install_fakes(monkeypatch: pytest.MonkeyPatch, *, fail_times: int = 0) -> dict[str, Any]:
    state: dict[str, Any] = {
        "fails": fail_times,
        "instances": [],
        "messages": [],
        "transports": [],
    }

    def make(transport: str) -> type:
        class FakeServer:
            def __init__(self, host: str, port: int, timeout: float = 0, context: Any = None):
                self.host = host
                self.port = port
                self.starttls_called = False
                self.logged_in: tuple[str, str] | None = None
                state["instances"].append(self)
                state["transports"].append(transport)

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def starttls(self, context: Any = None) -> None:
                self.starttls_called = True

            def login(self, user: str, password: str) -> None:
                self.logged_in = (user, password)

            def send_message(self, message: Any, from_addr: Any = None, to_addrs: Any = None) -> None:
                if state["fails"] > 0:
                    state["fails"] -= 1
                    raise smtplib.SMTPServerDisconnected("模拟断线")
                state["messages"].append((message, from_addr, to_addrs))

        return FakeServer

    monkeypatch.setattr(smtplib, "SMTP_SSL", make("ssl"))
    monkeypatch.setattr(smtplib, "SMTP", make("plain"))
    return state


def _send(cfg: SMTPConfig, sleeps: list[float], **overrides: Any):
    params: dict[str, Any] = {
        "to_email": "reader@test.com",
        "subject": "每日 AI 简报 · 2026-10-14",
        "text_body": "纯文本正文",
        "html_body": "<p>HTML 正文</p>",
    }
    params.update(overrides)
    return mailer.send_briefing(cfg, sleep=sleeps.append, **params)


def test_send_success_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fakes(monkeypatch)
    sleeps: list[float] = []
    result = _send(_cfg(), sleeps)

    assert result.ok and result.attempts == 1
    assert state["transports"] == ["ssl"]
    assert sleeps == []

    message, from_addr, to_addrs = state["messages"][0]
    assert from_addr == "sender@test.com"
    assert to_addrs == ["reader@test.com"]
    assert message["To"] == "reader@test.com"
    assert "News Agent" in str(message["From"])
    assert message.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in message.iter_parts()] == ["text/plain", "text/html"]
    assert state["instances"][0].logged_in == ("sender@test.com", "auth-code")


def test_send_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fakes(monkeypatch, fail_times=1)
    sleeps: list[float] = []
    result = _send(_cfg(), sleeps)

    assert result.ok and result.attempts == 2
    assert sleeps == [5.0]
    assert len(state["messages"]) == 1


def test_send_fails_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, fail_times=99)
    sleeps: list[float] = []
    result = _send(_cfg(), sleeps)

    assert not result.ok
    assert result.attempts == 3  # 1 次 + 2 次重试
    assert result.error and "SMTPServerDisconnected" in result.error
    assert sleeps == [5.0, 5.0]


def test_send_without_credentials_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fakes(monkeypatch)
    result = _send(_cfg(user="", password=""), [])

    assert not result.ok and result.attempts == 0
    assert result.error and "SMTP_USER" in result.error
    assert state["instances"] == []


def test_send_without_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fakes(monkeypatch)
    result = _send(_cfg(), [], to_email="   ")

    assert not result.ok and result.attempts == 0
    assert result.error and "收件人" in result.error
    assert state["instances"] == []


def test_starttls_for_non_ssl_port(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fakes(monkeypatch)
    result = _send(_cfg(port=587), [])

    assert result.ok
    assert state["transports"] == ["plain"]
    assert state["instances"][0].starttls_called is True


def test_retry_delay_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, fail_times=1)
    sleeps: list[float] = []
    result = _send(_cfg(retry_delay=0.5), sleeps)

    assert result.ok
    assert sleeps == [0.5]
