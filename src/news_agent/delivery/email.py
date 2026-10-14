"""SMTP 发送（docs §3.5）。

- 发件通道 = `SMTP_USER` + `SMTP_PASS`（授权码）；收件人 = 网页身份页的 `profile.email`，
  两者不混为一个字段；
- 端口 465 走 SSL，其余走 STARTTLS；
- 重试次数与间隔可配置（`SMTP_RETRIES` / `SMTP_RETRY_DELAY`）；
- 发送是编排层职责，**绝不注册为 Agent 工具**。
"""

from __future__ import annotations

import smtplib
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from ..config import SMTPConfig

SMTP_TIMEOUT = 30.0
MAX_ERROR_CHARS = 500


@dataclass(frozen=True)
class SendResult:
    ok: bool
    attempts: int
    error: str | None = None


def send_briefing(
    cfg: SMTPConfig,
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    sleep: Callable[[float], None] = time.sleep,
) -> SendResult:
    """发送简报邮件（text + html 双部分）。永不抛异常，结果放 `SendResult`。

    `sleep` 可注入（测试传 no-op），生产用 `time.sleep`。
    """
    to_email = (to_email or "").strip()
    if not to_email:
        return SendResult(
            ok=False, attempts=0, error="收件人邮箱为空：请先在网页「身份」页填写邮箱"
        )
    if not cfg.user or not cfg.password:
        return SendResult(
            ok=False,
            attempts=0,
            error="未配置 SMTP_USER / SMTP_PASS：请在 .env 中填写发件邮箱与授权码",
        )

    message = _build_message(
        cfg, to_email=to_email, subject=subject, text_body=text_body, html_body=html_body
    )

    total = max(1, cfg.retries + 1)
    last_error = ""
    for attempt in range(1, total + 1):
        try:
            _smtp_send(cfg, to_email=to_email, message=message)
            return SendResult(ok=True, attempts=attempt)
        except Exception as e:  # noqa: BLE001 - 网络与认证失败统一按可重试处理
            last_error = f"{type(e).__name__}: {e}"[:MAX_ERROR_CHARS]
            if attempt < total:
                sleep(max(0.0, cfg.retry_delay))
    return SendResult(ok=False, attempts=total, error=last_error)


def _smtp_send(cfg: SMTPConfig, *, to_email: str, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    if cfg.port == 465:
        with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=SMTP_TIMEOUT, context=context) as server:
            server.login(cfg.user, cfg.password)
            server.send_message(message, from_addr=cfg.user, to_addrs=[to_email])
    else:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT) as server:
            server.starttls(context=context)
            server.login(cfg.user, cfg.password)
            server.send_message(message, from_addr=cfg.user, to_addrs=[to_email])


def _build_message(
    cfg: SMTPConfig, *, to_email: str, subject: str, text_body: str, html_body: str
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((cfg.from_name, cfg.user))
    message["To"] = to_email
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message
