"""文件系统工具：list_dir / read_file / search_content / write_file

这四个是需求明文要求的工具。它们不只是"通用文件操作"——
按 docs §5.4，它们同时是 Agent 的**跨运行记忆通道**：
Agent 靠 `search_content` 检索 `data/briefings/` 来发现阶段性新闻的关联。

安全：所有路径解析后必须落在 `ALLOWED_ROOTS` 内，挡住 `../` 穿越。
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import DATA_DIR, WORKSPACE_DIR, ensure_dirs

ALLOWED_ROOTS = (DATA_DIR,)

# 输出上限：防止单个大文件把模型 context 撑爆
MAX_FILE_CHARS = 8_000
MAX_SEARCH_HITS = 30
SNIPPET_CHARS = 160
TEXT_SUFFIXES = {".md", ".txt", ".html", ".json", ".csv", ".log", ".yml", ".yaml"}


def _safe(fn: Callable[..., str]) -> Callable[..., str]:
    """把工具抛出的运行时异常转成错误文本，而不是让异常往外冒。

    `TypeError` 例外——那是参数名/个数写错了，交给 registry 单独提示模型，
    比笼统的"执行失败"更有用。

    这样无论经过 dispatch 还是被直接调用，工具的行为都一致：只返回字符串。
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except TypeError:
            raise
        except Exception as e:  # noqa: BLE001
            return f"[错误] {type(e).__name__}: {e}"

    return wrapper


def _resolve(path_str: str) -> Path:
    """把路径解析成绝对路径，并校验落在允许范围内。"""
    raw = Path(path_str)
    base = raw if raw.is_absolute() else WORKSPACE_DIR / raw
    resolved = base.resolve()

    for root in ALLOWED_ROOTS:
        if resolved == root or resolved.is_relative_to(root):
            return resolved

    raise PermissionError(
        f"路径越界：{path_str!r}（解析为 {resolved}）。"
        f"只允许访问：{', '.join(str(r) for r in ALLOWED_ROOTS)}"
    )


@_safe
def list_dir(path: str = "") -> str:
    """列出目录内容。相对路径以当日工作区为基准。"""
    d = _resolve(path)
    if not d.exists():
        return f"[错误] 目录不存在：{d}"
    if not d.is_dir():
        return f"[错误] 不是目录：{d}"

    entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    if not entries:
        return f"{d} 是空目录。"

    lines = [
        f"[DIR]  {e.name}/" if e.is_dir() else f"[FILE] {e.name}  ({e.stat().st_size} B)"
        for e in entries
    ]
    return f"{d} 下共 {len(entries)} 项：\n" + "\n".join(lines)


@_safe
def read_file(path: str) -> str:
    """读取文件正文。超长自动截断。"""
    p = _resolve(path)
    if not p.exists():
        return f"[错误] 文件不存在：{p}"
    if not p.is_file():
        return f"[错误] 不是文件：{p}"

    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_FILE_CHARS:
        return (
            text[:MAX_FILE_CHARS]
            + f"\n\n...(已截断，文件共 {len(text)} 字符；"
            f"如需看其余部分可用 search_content 定位)"
        )
    return text


@_safe
def search_content(keyword: str, dir: str = "") -> str:
    """在目录下按关键词搜索文件内容，返回命中片段。

    刻意只返回**片段**而不是整篇——docs §5.6 要求如此，
    否则模型查历史简报时会把全文读进来，token 直接爆炸。
    """
    d = _resolve(dir)
    if not d.is_dir():
        return f"[错误] 不是目录：{d}"

    hits: list[str] = []
    truncated = False

    for f in sorted(d.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        rel = f.relative_to(d)
        for lineno, line in enumerate(lines, start=1):
            if keyword.lower() in line.lower():
                snippet = line.strip()[:SNIPPET_CHARS]
                hits.append(f"{rel}:{lineno}: {snippet}")
                if len(hits) >= MAX_SEARCH_HITS:
                    truncated = True
                    break
        if truncated:
            break

    if not hits:
        return f"在 {d} 下没有找到包含「{keyword}」的内容。"

    head = f"在 {d} 下命中 {len(hits)} 处「{keyword}」："
    if truncated:
        head += f"（已达上限 {MAX_SEARCH_HITS} 条，建议换个更具体的关键词）"
    return head + "\n" + "\n".join(hits)


@_safe
def write_file(path: str, content: str) -> str:
    """写入文件，父目录自动创建。返回写入结果供模型确认。"""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {p}（{len(content)} 字符）。"


def ensure_workspace(date_str: str) -> Path:
    """准备当日工作区。幂等。"""
    ensure_dirs()
    d = WORKSPACE_DIR / date_str
    d.mkdir(parents=True, exist_ok=True)
    return d
