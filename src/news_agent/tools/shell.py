"""bash 工具：真实 Bash 子进程 + 分层约束。

需求指定保留 bash 命令工具；这里选择**真实执行**（Windows 用 Git for Windows 自带的
Git Bash），而不是用 Python 模拟命令——更贴合"让模型使用 shell 工具"的题意。
把 shell 交给模型必须层层设限，防护是分层的：

1. 命令白名单（ls/cat/grep/wc/date/head/tail），全部只读，不开放写入与网络；
2. 组合语法黑名单（管道 / 重定向 / `&` / `;` / 反引号 / `$` / 反斜杠 / 换行）；
3. 参数经 `shlex` 解析后在 Python 侧重建成 argv：路径一律 `_resolve()` 解析并锁在
   `data/` 内，数字等参数逐项校验——模型提供的 shell 语法在这一步已被拆散；
4. argv 再用 `shlex.join` 重新引用后交给 `bash -c 'exec ...'`：即便有字符绕过黑名单，
   也只会成为字面量参数，不会被执行；
5. 子进程环境清理（`--noprofile --norc`、受限 PATH）、工作目录锁在工作区、
   超时（5s）与输出上限（字节 + 字符双限，超限即终止）。

任何失败都返回可读错误文本供模型自纠，不向循环外抛异常（与其余工具一致）。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

from .filesystem import _resolve

ALLOWED_COMMANDS = {"ls", "cat", "grep", "wc", "date", "head", "tail"}
DENY_CHARS = ("|", ">", "<", "&", ";", "`", "$", "\\", "\n", "\r")
MAX_COMMAND_CHARS = 1_000
MAX_OUTPUT_BYTES = 32_000
MAX_OUTPUT_CHARS = 6_000
TIMEOUT_SECONDS = 5
MAX_LINES = 200


def bash(command: str) -> str:
    """执行一条白名单只读命令；路径必须落在 data/ 内，禁止任何 shell 组合语法。"""
    if not command or not command.strip():
        return "[错误] 空命令。"
    if len(command) > MAX_COMMAND_CHARS:
        return f"[拒绝] 命令超过 {MAX_COMMAND_CHARS} 字符上限。"

    hit = next((char for char in DENY_CHARS if char in command), None)
    if hit:
        return f"[拒绝] 命令包含被禁止的 shell 字符 {hit!r}。"

    try:
        parts = shlex.split(command, posix=True)
    except ValueError as exc:
        return f"[错误] 命令无法解析：{exc}"
    if not parts:
        return "[错误] 空命令。"

    name, *args = parts
    if name not in ALLOWED_COMMANDS:
        return f"[拒绝] 命令 {name!r} 不在白名单内。允许：{', '.join(sorted(ALLOWED_COMMANDS))}"

    try:
        argv = _build_argv(name, args)
        bash_path = _find_bash()
        env = _restricted_env(bash_path)
        # 调用时解析工作目录（跟随 filesystem 的配置，测试可注入）；新克隆/CI 上目录可能尚不存在。
        cwd = _resolve("")
        cwd.mkdir(parents=True, exist_ok=True)
        # Re-quote parsed arguments so shell syntax supplied by the model stays inert.
        script = "exec " + shlex.join(argv)
        return _execute(bash_path, script, env, cwd=cwd)
    except PermissionError as exc:
        return f"[拒绝] {exc}"
    except (FileNotFoundError, ValueError) as exc:
        return f"[错误] {exc}"
    except subprocess.TimeoutExpired:
        return f"[错误] 命令执行超过 {TIMEOUT_SECONDS} 秒，已终止。"
    except Exception as exc:  # noqa: BLE001 - tool errors are returned to the model
        return f"[错误] 命令执行失败：{type(exc).__name__}: {exc}"


def _build_argv(name: str, args: list[str]) -> list[str]:
    if name == "date":
        if args:
            raise ValueError("date 不接受参数")
        return ["date", "+%Y-%m-%d %H:%M:%S %Z"]

    if name == "ls":
        if len(args) > 1:
            raise ValueError("用法：ls [目录]")
        path = _checked_path(args[0]) if args else _checked_path("")
        return ["ls", "-la", "--", _shell_path(path)]

    if name in {"cat", "wc"}:
        if len(args) != 1:
            raise ValueError(f"用法：{name} <文件>")
        path = _checked_path(args[0], must_be_file=True)
        if name == "cat":
            return ["cat", "--", _shell_path(path)]
        return ["wc", "-l", "-w", "-m", "--", _shell_path(path)]

    if name == "grep":
        if len(args) != 2:
            raise ValueError("用法：grep <关键词> <文件或目录>")
        keyword, raw_path = args
        if not keyword:
            raise ValueError("grep 关键词不能为空")
        path = _checked_path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"路径不存在：{path}")
        return ["grep", "-r", "-n", "-F", "--", keyword, _shell_path(path)]

    if name in {"head", "tail"}:
        if len(args) == 1:
            line_count, raw_path = 10, args[0]
        elif len(args) == 3 and args[0] == "-n":
            try:
                line_count = int(args[1])
            except ValueError as exc:
                raise ValueError("-n 后必须是数字") from exc
            raw_path = args[2]
        else:
            raise ValueError(f"用法：{name} [-n 行数] <文件>")
        if not 1 <= line_count <= MAX_LINES:
            raise ValueError(f"行数必须在 1 到 {MAX_LINES} 之间")
        path = _checked_path(raw_path, must_be_file=True)
        return [name, "-n", str(line_count), "--", _shell_path(path)]

    raise ValueError(f"不支持的命令：{name}")


def _checked_path(raw_path: str, *, must_be_file: bool = False) -> Path:
    path = _resolve(raw_path)
    if must_be_file and not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    if not must_be_file and path.exists() and not (path.is_file() or path.is_dir()):
        raise PermissionError(f"只允许访问普通文件或目录：{path}")
    return path


def _shell_path(path: Path) -> str:
    # Git Bash accepts drive-letter paths with forward slashes.
    return path.as_posix()


def _find_bash() -> str:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parent.parent
            candidate = git_root / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
        raise FileNotFoundError("未找到 Git for Windows Bash；请安装 Git for Windows 后重试")

    path = shutil.which("bash")
    if path is None:
        raise FileNotFoundError("未找到 bash 可执行文件")
    return path


def _restricted_env(bash_path: str) -> dict[str, str]:
    if os.name == "nt":
        bash_dir = Path(bash_path).resolve().parent
        git_root = bash_dir.parent
        env = {
            key: os.environ[key]
            for key in ("SystemRoot", "SystemDrive", "WINDIR", "TEMP", "TMP")
            if key in os.environ
        }
        env["PATH"] = os.pathsep.join((str(bash_dir), str(git_root / "usr" / "bin")))
        return env
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def _execute(bash_path: str, script: str, env: dict[str, str], *, cwd: Path) -> str:
    proc = subprocess.Popen(
        [bash_path, "--noprofile", "--norc", "-c", script],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert proc.stdout is not None
    output = bytearray()
    truncated = threading.Event()

    def drain_output() -> None:
        while chunk := proc.stdout.read(4096):
            remaining = MAX_OUTPUT_BYTES - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated.set()
                proc.terminate()

    reader = threading.Thread(target=drain_output, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        reader.join()
        raise
    reader.join()

    text = output.decode("utf-8", errors="replace")
    if truncated.is_set():
        text += "\n...(输出超过字节上限，命令已终止)"
    if proc.returncode:
        text = f"[退出码 {proc.returncode}]\n{text}"
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n...(输出超过 {MAX_OUTPUT_CHARS} 字符上限)"
    return text or ("[完成]" if proc.returncode == 0 else f"[退出码 {proc.returncode}] 无输出")


__all__ = ["ALLOWED_COMMANDS", "bash"]
