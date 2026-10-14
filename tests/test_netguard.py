"""出网安全闸单测：SSRF 拒绝、逐跳复查、重定向上限、响应体上限。

不依赖真实网络：地址检查用 IP 字面量或假 DNS；HTTP 交互用 httpx.MockTransport。
"""

from __future__ import annotations

import socket
from collections.abc import Callable

import httpx
import pytest

from news_agent.tools import netguard

ALLOWED_HOSTS = {
    "public.test": "93.184.216.34",
    "second.test": "93.184.216.35",
    "internal.test": "10.1.2.3",
}


def _fake_getaddrinfo(host: str, port: int | None, *args: object, **kwargs: object) -> list:
    if host not in ALLOWED_HOSTS:
        raise socket.gaierror(f"unknown host: {host}")
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (ALLOWED_HOSTS[host], port or 0),
        )
    ]


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


def _mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=netguard.TIMEOUT,
        follow_redirects=False,
    )


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable], None]:
    """把 netguard 的共享客户端替换为 MockTransport。"""

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        monkeypatch.setattr(netguard, "_client", lambda: _mock_transport(handler))

    return install


# ---------- 地址检查 ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.8.8.8:8000/admin",
        "http://localhost/services",
        "http://10.0.0.1/x",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_check_url_blocks_non_public_addresses(url: str) -> None:
    assert netguard.check_url(url), f"未被拦截：{url}"


def test_check_url_allows_public_address(fake_dns: None) -> None:
    assert netguard.check_url("https://public.test/feed") is None


def test_check_url_rejects_bad_scheme_and_host(fake_dns: None) -> None:
    assert netguard.check_url("ftp://public.test/x")
    assert netguard.check_url("file:///etc/passwd")
    assert netguard.check_url("http://")
    assert netguard.check_url("http://nonexistent.test/x")  # 假 DNS 会 gaierror


# ---------- 抓取路径 ----------


def test_fetch_rejects_private_target_without_request(
    fake_dns: None, mock_client: Callable
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="ok")

    mock_client(handler)
    outcome = netguard.fetch("http://internal.test/secret")
    assert outcome.error and "已拒绝" in outcome.error
    assert seen == []  # 被拒绝的地址绝不能被请求


def test_fetch_follows_redirect_after_recheck(fake_dns: None, mock_client: Callable) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "public.test":
            return httpx.Response(302, headers={"location": "https://second.test/final"})
        return httpx.Response(
            200,
            text="正文内容",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    mock_client(handler)
    outcome = netguard.fetch("https://public.test/start")
    assert outcome.error is None
    assert outcome.text == "正文内容"
    assert outcome.url == "https://second.test/final"
    assert len(seen) == 2


def test_fetch_blocks_redirect_to_private_address(fake_dns: None, mock_client: Callable) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://internal.test/secret"})

    mock_client(handler)
    outcome = netguard.fetch("https://public.test/start")
    assert outcome.error and "已拒绝" in outcome.error
    assert len(seen) == 1  # 第二跳在发出前就被拦下


def test_fetch_limits_redirect_count(fake_dns: None, mock_client: Callable) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://public.test/loop"})

    mock_client(handler)
    outcome = netguard.fetch("https://public.test/loop")
    assert outcome.error and "重定向次数" in outcome.error


def test_fetch_truncates_oversized_body(fake_dns: None, mock_client: Callable) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 500)

    mock_client(handler)
    outcome = netguard.fetch("https://public.test/big", max_bytes=100)
    assert outcome.error is None
    assert outcome.truncated is True
    assert outcome.text is not None and len(outcome.text) == 100


def test_fetch_reports_http_error(fake_dns: None, mock_client: Callable) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    mock_client(handler)
    outcome = netguard.fetch("https://public.test/missing")
    assert outcome.error == "HTTP 404"


def test_fetch_reports_transport_error(fake_dns: None, mock_client: Callable) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_client(handler)
    outcome = netguard.fetch("https://public.test/x")
    assert outcome.error and "ConnectError" in outcome.error
