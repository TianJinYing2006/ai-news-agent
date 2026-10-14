"""存储层：SQLite（WAL）+ 偏好读写 + 简报状态机 + 原子认领。

契约见 docs/implementation-plan.md §3.1 / §3.2。两条关键设计：

1. **认领必须原子**：`claim_daily_run` 在 `BEGIN IMMEDIATE` 事务里完成
   "读状态 → 判分支 → 写新状态"，不能把"先查 sent、稍后再写"当锁。
   并发触发靠 SQLite 的写锁串行化，第二个调用看到的是第一个刚提交的状态。
2. **失败不让状态悬空**：`mark_failed` 记录阶段与错误；下一次 claim 依据
   状态、陈旧租约（120 分钟）和产物可用性决定"重新生成"还是"只重发邮件"，
   SMTP 失败重试不会重跑 LLM（见 §3.2 第 4 条）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import BRIEFINGS_DIR, DB_PATH

TZ = ZoneInfo("Asia/Shanghai")

STALE_AFTER_MINUTES = 120

DEFAULT_MAX_ITEMS = 10
LAUNCH_MAX_ITEMS = 50
MAX_LIST_ITEMS = 100
MAX_ITEM_CHARS = 80
MAX_NAME_CHARS = 100
MAX_SUMMARY_CHARS = 2000
MAX_ERROR_CHARS = 2000

FAILURE_STAGE_SMTP = "smtp"

CLAIM_SKIPPED = "skipped"
CLAIM_ALREADY_RUNNING = "already_running"
CLAIM_GENERATE = "generate"
CLAIM_RETRY_SEND = "retry_send"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS profile (
        id         INTEGER PRIMARY KEY CHECK (id = 1),
        name       TEXT NOT NULL,
        email      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscription (
        id                    INTEGER PRIMARY KEY CHECK (id = 1),
        topics_json           TEXT NOT NULL,
        keywords_json         TEXT NOT NULL,
        exclude_keywords_json TEXT NOT NULL,
        max_items             INTEGER NOT NULL,
        updated_at            TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS briefings (
        date          TEXT PRIMARY KEY,
        status        TEXT NOT NULL CHECK (
            status IN ('generating', 'generated', 'sending', 'sent', 'failed')
        ),
        md_path       TEXT NOT NULL,
        html_path     TEXT NOT NULL,
        summary       TEXT,
        item_count    INTEGER,
        attempt       INTEGER NOT NULL DEFAULT 0,
        started_at    TEXT,
        sent_at       TEXT,
        failure_stage TEXT,
        error         TEXT,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        date       TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at   TEXT NOT NULL,
        status     TEXT NOT NULL,
        steps      INTEGER NOT NULL DEFAULT 0,
        tool_calls INTEGER NOT NULL DEFAULT 0,
        tokens_in  INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        trace_json TEXT NOT NULL,
        error      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_date ON agent_runs(date)",
)


class Database:
    """SQLite 连接包装。

    - `isolation_level=None` 关闭隐式事务，写路径统一走 `transaction()`；
    - WAL + `busy_timeout=10s` 支撑"定时任务与手动运行撞车"的场景（docs §7.1）。
    """

    def __init__(
        self,
        path: Path | str = DB_PATH,
        *,
        briefings_dir: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：Web（uvicorn/TestClient 线程池）与 CLI 会跨线程使用同一连接；
        # sqlite3 模块是 serialized 线程模式，连接级并发由 sqlite 串行化，写事务仍走 BEGIN IMMEDIATE。
        self.conn = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        # 简报产物所在目录：claim 用它推导当天预期路径、校验重试产物。
        self.briefings_dir = Path(briefings_dir) if briefings_dir else BRIEFINGS_DIR

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """`BEGIN IMMEDIATE` 事务：立即取得写锁，读-改-写整体原子。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        cur = self.conn.execute(sql, params)
        try:
            row = cur.fetchone()
        finally:
            cur.close()
        return dict(row) if row is not None else None

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.conn.execute(sql, params)
        try:
            rows = cur.fetchall()
        finally:
            cur.close()
        return [dict(r) for r in rows]

    def execute(self, sql: str, params: tuple = ()) -> int:
        """执行写语句（autocommit），返回受影响行数。"""
        cur = self.conn.execute(sql, params)
        try:
            return cur.rowcount
        finally:
            cur.close()


def init_db(db: Database) -> None:
    """建表。幂等，可重复调用。"""
    with db.transaction():
        for stmt in _SCHEMA:
            db.conn.execute(stmt)


# ---------------------------------------------------------------------------
# 身份与订阅偏好
# ---------------------------------------------------------------------------


def save_profile(db: Database, name: str, email: str) -> None:
    name = (name or "").strip()
    email = (email or "").strip()
    if not name:
        raise ValueError("姓名不能为空")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"姓名过长（超过 {MAX_NAME_CHARS} 字符）")
    if not _EMAIL_RE.match(email):
        raise ValueError(f"邮箱格式不正确：{email!r}")

    db.execute(
        """
        INSERT INTO profile(id, name, email, updated_at) VALUES(1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name, email = excluded.email, updated_at = excluded.updated_at
        """,
        (name, email, _now_iso()),
    )


def get_profile(db: Database) -> dict | None:
    return db.fetchone("SELECT name, email, updated_at FROM profile WHERE id = 1")


def save_subscription(
    db: Database,
    topics: list[str],
    keywords: list[str],
    exclude_keywords: list[str],
    max_items: int,
) -> None:
    topics = _clean_str_list(topics, "话题")
    keywords = _clean_str_list(keywords, "关键词")
    exclude_keywords = _clean_str_list(exclude_keywords, "排除词")
    if isinstance(max_items, bool) or not isinstance(max_items, int):
        raise TypeError("max_items 必须是整数")
    if not 1 <= max_items <= LAUNCH_MAX_ITEMS:
        raise ValueError(f"max_items 必须在 1–{LAUNCH_MAX_ITEMS} 之间")

    db.execute(
        """
        INSERT INTO subscription(id, topics_json, keywords_json, exclude_keywords_json,
                                 max_items, updated_at)
        VALUES(1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            topics_json = excluded.topics_json,
            keywords_json = excluded.keywords_json,
            exclude_keywords_json = excluded.exclude_keywords_json,
            max_items = excluded.max_items,
            updated_at = excluded.updated_at
        """,
        (
            json.dumps(topics, ensure_ascii=False),
            json.dumps(keywords, ensure_ascii=False),
            json.dumps(exclude_keywords, ensure_ascii=False),
            max_items,
            _now_iso(),
        ),
    )


def get_subscription(db: Database) -> dict:
    row = db.fetchone(
        "SELECT topics_json, keywords_json, exclude_keywords_json, max_items, updated_at "
        "FROM subscription WHERE id = 1"
    )
    if row is None:
        return {
            "topics": [],
            "keywords": [],
            "exclude_keywords": [],
            "max_items": DEFAULT_MAX_ITEMS,
            "updated_at": None,
        }
    return {
        "topics": json.loads(row["topics_json"]),
        "keywords": json.loads(row["keywords_json"]),
        "exclude_keywords": json.loads(row["exclude_keywords_json"]),
        "max_items": row["max_items"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# 简报状态机与原子认领
# ---------------------------------------------------------------------------


def claim_daily_run(
    db: Database,
    date: str,
    stale_after_minutes: int = STALE_AFTER_MINUTES,
) -> str:
    """原子认领当天运行，返回下一步动作（见 §3.2）：

    - `skipped`：已发送，不再重复发送；
    - `already_running`：租约内已有运行在跑（generating/generated/sending）；
    - `retry_send`：产物可用，只需重发邮件，**不重跑 LLM**；
    - `generate`：需要完整生成（首次、模型阶段失败、产物缺失或租约过期）。
    """
    _validate_date(date)
    now = _now()
    now_iso = _iso(now)
    md_path, html_path = _expected_paths(db.briefings_dir, date)

    with db.transaction():
        row = db.fetchone("SELECT * FROM briefings WHERE date = ?", (date,))

        if row is None:
            db.execute(
                "INSERT INTO briefings(date, status, md_path, html_path, attempt, started_at,"
                " updated_at) VALUES(?, 'generating', ?, ?, 1, ?, ?)",
                (date, md_path, html_path, now_iso, now_iso),
            )
            return CLAIM_GENERATE

        status = row["status"]
        if status == "sent":
            return CLAIM_SKIPPED

        stale = _is_stale(row["started_at"], now, stale_after_minutes)

        if status in ("generating", "generated", "sending") and not stale:
            return CLAIM_ALREADY_RUNNING

        in_flight_stale = status in ("generated", "sending") and stale
        smtp_failed = status == "failed" and row["failure_stage"] == FAILURE_STAGE_SMTP
        if (
            in_flight_stale or smtp_failed
        ) and _artifacts_usable(db.briefings_dir, row["md_path"], row["html_path"]):
            db.execute(
                "UPDATE briefings SET status = 'sending', attempt = attempt + 1,"
                " started_at = ?, failure_stage = NULL, error = NULL, updated_at = ?"
                " WHERE date = ?",
                (now_iso, now_iso, date),
            )
            return CLAIM_RETRY_SEND

        # 首次由上面处理；这里是：过期的 generating、模型阶段失败、
        # SMTP 失败但产物缺失/不合格 —— 一律完整重生成。
        db.execute(
            "UPDATE briefings SET status = 'generating', attempt = attempt + 1,"
            " summary = NULL, item_count = NULL, failure_stage = NULL, error = NULL,"
            " updated_at = ? WHERE date = ?",
            (now_iso, date),
        )
        return CLAIM_GENERATE


def mark_generated(
    db: Database,
    date: str,
    *,
    md_path: str,
    html_path: str,
    summary: str,
    item_count: int,
) -> None:
    """生成完成落库（generating → generated）。路径必须在简报目录内。"""
    _validate_date(date)
    for label, raw in (("md_path", md_path), ("html_path", html_path)):
        if not _path_in_briefings_dir(db.briefings_dir, raw):
            raise ValueError(f"{label} 必须位于简报目录 {db.briefings_dir} 内：{raw!r}")
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("summary 不能为空")
    if len(summary) > MAX_SUMMARY_CHARS:
        raise ValueError(f"summary 过长（超过 {MAX_SUMMARY_CHARS} 字符）")
    if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 0:
        raise ValueError("item_count 必须是非负整数")

    affected = db.execute(
        "UPDATE briefings SET status = 'generated', md_path = ?, html_path = ?, summary = ?,"
        " item_count = ?, failure_stage = NULL, error = NULL, updated_at = ?"
        " WHERE date = ? AND status = 'generating'",
        (md_path, html_path, summary, item_count, _now_iso(), date),
    )
    if affected == 0:
        raise RuntimeError(f"mark_generated：{date} 不存在或当前状态不是 generating")


def mark_sending(db: Database, date: str) -> None:
    """发送前标记（generated → sending）。"""
    _validate_date(date)
    affected = db.execute(
        "UPDATE briefings SET status = 'sending', updated_at = ?"
        " WHERE date = ? AND status = 'generated'",
        (_now_iso(), date),
    )
    if affected == 0:
        raise RuntimeError(f"mark_sending：{date} 不存在或当前状态不是 generated")


def mark_sent(db: Database, date: str) -> None:
    """SMTP 已接受（generated/sending → sent）。sent 是终态。"""
    _validate_date(date)
    now_iso = _now_iso()
    affected = db.execute(
        "UPDATE briefings SET status = 'sent', sent_at = ?, failure_stage = NULL, error = NULL,"
        " updated_at = ? WHERE date = ? AND status IN ('generated', 'sending')",
        (now_iso, now_iso, date),
    )
    if affected == 0:
        raise RuntimeError(f"mark_sent：{date} 不存在、已发送或状态不允许发送")


def mark_failed(db: Database, date: str, stage: str, error: str) -> None:
    """任一阶段失败 → failed + 阶段 + 错误。已发送的简报不允许改写。"""
    _validate_date(date)
    stage = (stage or "").strip()
    error = (error or "").strip()
    if not stage:
        raise ValueError("failure stage 不能为空")
    if not error:
        raise ValueError("error 不能为空")
    error = error[:MAX_ERROR_CHARS]

    affected = db.execute(
        "UPDATE briefings SET status = 'failed', failure_stage = ?, error = ?, updated_at = ?"
        " WHERE date = ? AND status != 'sent'",
        (stage, error, _now_iso(), date),
    )
    if affected == 0:
        raise RuntimeError(f"mark_failed：{date} 不存在，或已 sent（sent 是终态，不允许改写）")


def insert_run(
    db: Database,
    date: str,
    started_at: str,
    ended_at: str,
    status: str,
    trace: list[dict],
    error: str | None,
    *,
    steps: int = 0,
    tool_calls: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> int:
    """保存一次 Agent 运行的统计与精简 trace，返回自增 id。

    脱敏由调用方（pipeline）负责：这里不落 API key、SMTP 凭据或完整私密正文。
    """
    _validate_date(date)
    cur = db.conn.execute(
        "INSERT INTO agent_runs(date, started_at, ended_at, status, steps, tool_calls,"
        " tokens_in, tokens_out, trace_json, error) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            date,
            started_at,
            ended_at,
            status,
            steps,
            tool_calls,
            tokens_in,
            tokens_out,
            json.dumps(trace, ensure_ascii=False),
            error,
        ),
    )
    try:
        return int(cur.lastrowid or 0)
    finally:
        cur.close()


def get_briefing(db: Database, date: str) -> dict | None:
    """读取当天简报记录（pipeline 与网页历史页使用）。"""
    _validate_date(date)
    return db.fetchone("SELECT * FROM briefings WHERE date = ?", (date,))


def list_briefings(db: Database, limit: int = 30) -> list[dict]:
    """按日期倒序列出简报记录（网页历史页使用，不返回正文——正文在文件里）。"""
    limit = max(1, min(int(limit), 200))
    return db.fetchall(
        "SELECT date, status, summary, item_count, attempt, sent_at, failure_stage, error,"
        " updated_at FROM briefings ORDER BY date DESC LIMIT ?",
        (limit,),
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(TZ)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _now_iso() -> str:
    return _iso(_now())


def _validate_date(date: str) -> None:
    """日期必须是 YYYY-MM-DD —— 它会直接参与文件名拼接，必须挡住注入。"""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(f"日期必须形如 YYYY-MM-DD，收到：{date!r}") from None


def _expected_paths(briefings_dir: Path, date: str) -> tuple[str, str]:
    return (str(briefings_dir / f"{date}.md"), str(briefings_dir / f"{date}.html"))


def _is_stale(started_at: str | None, now: datetime, minutes: int) -> bool:
    """租约判断。缺时间戳或解析失败按"陈旧"处理，保证不会永久卡住。"""
    if not started_at:
        return True
    try:
        ts = datetime.fromisoformat(started_at)
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=TZ)
    return (now - ts).total_seconds() > minutes * 60


def _path_in_briefings_dir(briefings_dir: Path, raw: str | None) -> bool:
    if not raw:
        return False
    path = Path(raw)
    if not path.is_absolute():
        return False
    try:
        return path.resolve().is_relative_to(briefings_dir.resolve())
    except OSError:
        return False


def _artifacts_usable(briefings_dir: Path, md_path: str | None, html_path: str | None) -> bool:
    """重发前对产物的轻量检查：路径在简报目录内、存在且非空。

    完整内容校验（标题、链接、条数）由 pipeline 在发送前执行（§3.5）。
    """
    for raw in (md_path, html_path):
        if not _path_in_briefings_dir(briefings_dir, raw):
            return False
        path = Path(raw).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            return False
    return True


def _clean_str_list(values: list[str], field: str) -> list[str]:
    if not isinstance(values, list):
        raise TypeError(f"{field}必须是字符串列表")
    cleaned: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            raise TypeError(f"{field}中的每一项必须是字符串，收到 {type(v).__name__}")
        v = v.strip()
        if not v:
            continue
        if len(v) > MAX_ITEM_CHARS:
            raise ValueError(f"{field}单项过长（超过 {MAX_ITEM_CHARS} 字符）：{v[:20]}…")
        if v not in seen:
            seen.add(v)
            cleaned.append(v)
    if len(cleaned) > MAX_LIST_ITEMS:
        raise ValueError(f"{field}条数过多（超过 {MAX_LIST_ITEMS} 条）")
    return cleaned
