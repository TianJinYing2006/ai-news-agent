"""工具层单测。

重点覆盖 docs §8 风险表里的几条：
- 风险 2：bash 被模型用来搞破坏 → 白名单 + 危险字符 + 路径越界
- 风险 4：非法 JSON 参数 → 错误回传给模型而不是让循环崩掉（docs §3 坑 4）
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from news_agent.tools import builtin, filesystem, netguard, news, shell
from news_agent.tools.registry import ToolRegistry


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把文件工具的工作根重定向到临时目录，避免污染真实的 data/。"""
    monkeypatch.setattr(filesystem, "ALLOWED_ROOTS", (tmp_path,))
    monkeypatch.setattr(filesystem, "WORKSPACE_DIR", tmp_path)
    return tmp_path


# ---------- 安全边界 ----------


def test_path_traversal_rejected(sandbox: Path) -> None:
    """../ 穿越必须被挡住（docs §8 风险 2）。"""
    for evil in ("../../etc/passwd", "..\\..\\Windows\\win.ini", "/etc/passwd"):
        out = filesystem.read_file(evil)
        assert "越界" in out or "拒绝" in out or "不存在" in out, f"未被拦截：{evil}"


def test_write_outside_root_rejected(sandbox: Path, tmp_path: Path) -> None:
    """工具只返回错误文本，绝不向外抛异常——直接调用和经 dispatch 调用行为一致。"""
    outside = tmp_path.parent / "evil.txt"
    out = filesystem.write_file(str(outside), "x")
    assert "越界" in out
    assert not outside.exists()


# ---------- bash 白名单 ----------


def test_bash_rejects_dangerous_chars() -> None:
    for cmd in ["ls | grep x", "cat a > b", "ls; rm -rf /", "echo $HOME", "cat `whoami`"]:
        out = shell.bash(cmd)
        assert "拒绝" in out, f"未被拦截：{cmd}"


def test_bash_rejects_non_whitelisted_command() -> None:
    out = shell.bash("rm important.md")
    assert "不在白名单" in out


def test_bash_whitelisted_runs(sandbox: Path) -> None:
    (sandbox / "a.md").write_text("hello\nworld\n", encoding="utf-8")
    assert "a.md" in shell.bash("ls")
    assert "hello" in shell.bash("cat a.md")
    assert "2 行" in shell.bash("wc a.md")


# ---------- registry 的错误回传（不崩循环） ----------


def _registry() -> ToolRegistry:
    return builtin.build_registry()


def test_invalid_json_argument_returns_error_text() -> None:
    """坑 4：arguments 不是合法 JSON 时，把错误回传给模型让它重试。"""
    out = _registry().dispatch("read_file", "{not json")
    assert "不是合法 JSON" in out
    assert "请重新调用" in out


def test_unknown_tool_returns_error_text() -> None:
    out = _registry().dispatch("delete_everything", "{}")
    assert "未知工具" in out
    assert "read_file" in out  # 顺带告诉模型有哪些可用


def test_wrong_argument_name_returns_error_text(sandbox: Path) -> None:
    out = _registry().dispatch("read_file", json.dumps({"filename": "a.md"}))
    assert "参数不匹配" in out


def test_tool_exception_is_captured() -> None:
    """工具内部抛异常时也要转成文本，绝不让循环崩掉。"""

    def boom() -> str:
        raise RuntimeError("模拟工具内部炸了")

    reg = ToolRegistry()
    reg.register(
        __import__("news_agent.tools.registry", fromlist=["Tool"]).Tool(
            name="boom", description="x", parameters={"type": "object", "properties": {}}, fn=boom
        )
    )
    out = reg.dispatch("boom", "{}")
    assert "执行失败" in out and "RuntimeError" in out


# ---------- 基本功能 ----------


def test_write_then_read_roundtrip(sandbox: Path) -> None:
    assert "已写入" in filesystem.write_file("notes.md", "你好，世界")
    assert filesystem.read_file("notes.md") == "你好，世界"


def test_search_content_returns_snippets_not_whole_file(sandbox: Path) -> None:
    """docs §5.6：只返回命中片段，不能把整篇喂回去。"""
    (sandbox / "long.md").write_text(
        "\n".join(f"第{i}行无关内容padding padding padding" for i in range(50)),
        encoding="utf-8",
    )
    (sandbox / "long.md").write_text(
        (sandbox / "long.md").read_text(encoding="utf-8") + "\n这一行提到了 GPT-5 发布",
        encoding="utf-8",
    )
    out = filesystem.search_content("GPT-5", "")
    assert "GPT-5" in out
    assert "命中 1 处" in out
    assert len(out) < 500  # 确认不是整篇


def test_search_content_miss_reports_clearly(sandbox: Path) -> None:
    (sandbox / "a.md").write_text("无关内容", encoding="utf-8")
    assert "没有找到" in filesystem.search_content("量子计算", "")


def test_list_dir_lists_entries(sandbox: Path) -> None:
    (sandbox / "x.md").write_text("hi", encoding="utf-8")
    (sandbox / "sub").mkdir()
    out = filesystem.list_dir("")
    assert "[FILE] x.md" in out
    assert "[DIR]  sub/" in out


# ---------- schema 结构 ----------


def test_registry_exposes_nine_tools() -> None:
    reg = _registry()
    assert len(reg.names()) == 9
    assert set(reg.names()) == {
        "list_dir",
        "read_file",
        "search_content",
        "write_file",
        "bash",
        "fetch_rss",
        "search_web",
        "fetch_url",
        "finish",
    }


def test_schemas_match_deepseek_format() -> None:
    """每个工具都必须是 DeepSeek 要求的 {"type":"function","function":{...}} 结构。"""
    for s in _registry().schemas():
        assert s["type"] == "function"
        fn = s["function"]
        assert set(fn) == {"name", "description", "parameters"}
        assert fn["parameters"]["type"] == "object"
        assert fn["description"].strip()


# ---------- 出网安全闸（fetch_url / fetch_rss 经 netguard，docs §8 风险表） ----------


def _fake_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    def fake(host: str, port: int | None, *args: object, **kwargs: object) -> list[tuple]:
        if host not in mapping:
            raise socket.gaierror(f"unknown host: {host}")
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (mapping[host], port or 0),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _mock_fetch_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    monkeypatch.setattr(
        netguard,
        "_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(handler),
            timeout=netguard.TIMEOUT,
            follow_redirects=False,
        ),
    )


def test_fetch_url_blocks_private_target() -> None:
    """模型给出的 loopback 地址必须被拦下，且不发出请求。"""
    out = news.fetch_url("http://127.0.0.1:8000/")
    assert "拒绝" in out


def test_fetch_url_extracts_main_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch, {"site.test": "93.184.216.34"})
    html = (
        "<html><head><title>标题</title></head><body><article><p>"
        + "人工智能新闻内容。" * 40
        + "</p></article></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=html, headers={"content-type": "text/html; charset=utf-8"}
        )

    _mock_fetch_client(monkeypatch, handler)
    out = news.fetch_url("https://site.test/a")
    assert "正文（来自" in out
    assert "人工智能" in out


def test_fetch_rss_rejects_oversized_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_dns(monkeypatch, {"feed.test": "93.184.216.34"})
    monkeypatch.setattr(netguard, "MAX_RESPONSE_BYTES", 64)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<rss>" + "x" * 500, headers={"content-type": "application/rss+xml"}
        )

    _mock_fetch_client(monkeypatch, handler)
    out = news.fetch_rss("https://feed.test/rss")
    assert "大小上限" in out
