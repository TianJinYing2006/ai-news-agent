"""出网安全闸：SSRF 防护 + 统一超时 / 响应体上限。

背景：`fetch_url` / `fetch_rss` 的 URL 由模型提供，属于不可信输入。
直接请求意味着模型可以读到 loopback 上的本机服务、私网设备、或
link-local 的云元数据端点（169.254.169.254）——即 SSRF（docs §8 风险表）。

防护分四层：

1. 只允许 `http` / `https`，且必须带主机名；
2. DNS 解析后逐个检查地址，任一落在非公网段即整体拒绝
   （loopback / 私网 / link-local / 保留 / 组播 / 未指定，含 IPv4-mapped IPv6）；
3. 关闭 httpx 自动跳转，手写重定向循环，**每一跳重新做地址检查**
   —— `follow_redirects=True` 时无法逐跳复查，是常见绕过点；
4. 连接 10s / 总 20s 超时；响应体读满上限即截断，避免超大响应拖垮内存。

已知边界：检查与真正连接之间存在理论上的 DNS rebinding 窗口（TOCTOU）。
本项目单机单用户、URL 来自模型而非外部攻击者，接受该边界并在此注明；
若未来对外提供服务，需改为"先解析、后按已验证 IP 连接"。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from functools import lru_cache

import httpx

UA = "Mozilla/5.0 (compatible; NewsAgent/0.1; +https://github.com/)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2_000_000


@dataclass(frozen=True)
class FetchOutcome:
    """一次带防护的抓取结果。`error` 非空即失败，文本永不抛异常。"""

    url: str
    text: str | None
    error: str | None
    truncated: bool = False


@lru_cache(maxsize=1)
def _client() -> httpx.Client:
    """共享 HTTP 客户端。

    - `trust_env=False`：本机代理对所有目标站点不通，必须直连（docs §2 实测）；
    - `follow_redirects=False`：重定向必须由 `fetch` 逐跳复查地址后再请求。
    """
    return httpx.Client(
        trust_env=False,
        timeout=TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": UA},
    )


def check_url(url: str) -> str | None:
    """URL 静态检查 + DNS 地址检查。通过返回 None，否则返回拒绝原因。"""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError) as e:
        return f"URL 无法解析：{e}"

    if parsed.scheme not in ("http", "https"):
        return f"只允许 http/https，收到 scheme={parsed.scheme!r}"
    if not parsed.host:
        return "URL 缺少主机名"
    return _check_host(parsed.host)


def _check_host(host: str) -> str | None:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return f"DNS 解析失败：{e}"

    for info in infos:
        addr = _parse_addr(info[4][0])
        if addr is None:
            return f"无法识别的目标地址：{info[4][0]!r}"
        if not addr.is_global:
            return f"目标地址 {addr} 不是公网地址（拒绝 loopback / 私网 / link-local / 保留段）"
    return None


def _parse_addr(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def fetch(url: str, *, max_bytes: int | None = None) -> FetchOutcome:
    """带 SSRF 防护的 GET。逐跳复查地址；失败返回可读错误文本。"""
    limit = MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
    client = _client()
    current = url

    for _ in range(MAX_REDIRECTS + 1):
        denied = check_url(current)
        if denied:
            return FetchOutcome(url=current, text=None, error=f"已拒绝访问：{denied}")

        try:
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    try:
                        current = str(resp.url.join(location))
                    except (httpx.InvalidURL, ValueError) as e:
                        return FetchOutcome(
                            url=current, text=None, error=f"重定向目标无法解析：{e}"
                        )
                    continue
                if 300 <= resp.status_code < 400:
                    return FetchOutcome(
                        url=current, text=None, error=f"HTTP {resp.status_code}（未预期的 3xx 状态）"
                    )
                resp.raise_for_status()

                chunks: list[bytes] = []
                size = 0
                truncated = False
                for chunk in resp.iter_bytes(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= limit:
                        truncated = True
                        break

                data = b"".join(chunks)[:limit]
                encoding = resp.charset_encoding or "utf-8"
                return FetchOutcome(
                    url=current,
                    text=data.decode(encoding, errors="replace"),
                    error=None,
                    truncated=truncated,
                )
        except httpx.HTTPStatusError as e:
            return FetchOutcome(url=current, text=None, error=f"HTTP {e.response.status_code}")
        except httpx.HTTPError as e:
            return FetchOutcome(url=current, text=None, error=f"{type(e).__name__}: {e}")

    return FetchOutcome(url=current, text=None, error=f"重定向次数超过上限（{MAX_REDIRECTS} 次）")
