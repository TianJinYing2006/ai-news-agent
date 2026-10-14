"""bash 工具：白名单 + 跨平台。

两个刻意的设计：

1. **不经过真正的 shell**。`shell=False` + 用 Python 实现命令语义，
   从根上杜绝 shell 注入 —— 这是把 bash 交给 LLM 时最大的风险（docs §8 风险 2）。
2. **跨平台**。Windows 上并没有 `ls` / `cat` / `grep`，若真起一个 shell 会直接失败。
   这里接受类 Unix 语法，内部用标准库执行，行为在 Windows / Linux / macOS 上一致。
"""

from __future__ import annotations

import shlex
from datetime import datetime

from .filesystem import _resolve

ALLOWED_COMMANDS = {"ls", "cat", "grep", "wc", "date", "head", "tail"}
DENY_CHARS = ("|", ">", "<", "&", ";", "`", "$", "\n", "\r")

MAX_OUTPUT_CHARS = 6_000
MAX_LINES = 200


def bash(command: str) -> str:
    """执行受限的 shell 命令。白名单之外一律拒绝，不静默。"""
    if not command or not command.strip():
        return "[错误] 空命令。"

    hit = next((c for c in DENY_CHARS if c in command), None)
    if hit:
        return (
            f"[拒绝] 命令包含被禁止的字符 {hit!r}。"
            "管道、重定向、命令拼接、变量展开均已禁用。"
        )

    try:
        parts = shlex.split(command)
    except ValueError as e:
        return f"[错误] 命令无法解析：{e}"

    if not parts:
        return "[错误] 空命令。"

    cmd, args = parts[0], parts[1:]
    if cmd not in ALLOWED_COMMANDS:
        return (
            f"[拒绝] 命令 {cmd!r} 不在白名单内。允许的命令："
            f"{', '.join(sorted(ALLOWED_COMMANDS))}"
        )

    try:
        return _clip(_DISPATCH[cmd](args))
    except PermissionError as e:
        return f"[拒绝] {e}"
    except FileNotFoundError as e:
        return f"[错误] 文件不存在：{e}"
    except Exception as e:  # noqa: BLE001 - 错误回传给模型，不中断循环
        return f"[错误] {cmd} 执行失败：{type(e).__name__}: {e}"


# ---------- 各命令的 Python 实现 ----------


def _cmd_ls(args: list[str]) -> str:
    target = _resolve(args[0]) if args else _resolve("")
    if not target.is_dir():
        return f"[错误] 不是目录：{target}"
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    if not entries:
        return f"{target} 是空目录。"
    return "\n".join(f"{e.name}{'/' if e.is_dir() else ''}" for e in entries)


def _cmd_cat(args: list[str]) -> str:
    if not args:
        return "[错误] cat 需要至少一个文件路径。"
    chunks = []
    for a in args:
        p = _resolve(a)
        if not p.is_file():
            raise FileNotFoundError(p)
        chunks.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def _cmd_grep(args: list[str]) -> str:
    if len(args) < 2:
        return "[错误] 用法：grep <关键词> <文件或目录>"
    keyword, target = args[0], _resolve(args[1])
    files = [target] if target.is_file() else [f for f in target.rglob("*") if f.is_file()]

    hits: list[str] = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = f.name if f == target else str(f.relative_to(target))
        for lineno, line in enumerate(lines, start=1):
            if keyword.lower() in line.lower():
                hits.append(f"{rel}:{lineno}: {line.strip()[:160]}")
                if len(hits) >= MAX_LINES:
                    return "\n".join(hits) + f"\n...(已达 {MAX_LINES} 行上限)"
    return "\n".join(hits) if hits else f"没有找到包含「{keyword}」的内容。"


def _cmd_wc(args: list[str]) -> str:
    if not args:
        return "[错误] wc 需要文件路径。"
    p = _resolve(args[0])
    if not p.is_file():
        raise FileNotFoundError(p)
    text = p.read_text(encoding="utf-8", errors="replace")
    return f"{len(text.splitlines())} 行  {len(text.split())} 词  {len(text)} 字符  {p.name}"


def _cmd_date(args: list[str]) -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")


def _head_tail(args: list[str], tail: bool) -> str:
    if not args:
        return "[错误] 需要文件路径。"
    n = 10
    path_arg = args[-1]
    if len(args) >= 3 and args[0] == "-n":
        try:
            n = int(args[1])
        except ValueError:
            return f"[错误] -n 后面必须是数字，收到 {args[1]!r}"
    p = _resolve(path_arg)
    if not p.is_file():
        raise FileNotFoundError(p)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:] if tail else lines[:n])


_DISPATCH = {
    "ls": _cmd_ls,
    "cat": _cmd_cat,
    "grep": _cmd_grep,
    "wc": _cmd_wc,
    "date": _cmd_date,
    "head": lambda a: _head_tail(a, tail=False),
    "tail": lambda a: _head_tail(a, tail=True),
}


def _clip(text: str) -> str:
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + f"\n...(输出已截断，共 {len(text)} 字符)"
    return text


__all__ = ["bash", "ALLOWED_COMMANDS"]
