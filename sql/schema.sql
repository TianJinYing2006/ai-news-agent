-- 每日 AI 新闻助手 · SQLite 建表脚本（幂等，可重复执行）
-- 与应用代码里的建表语句（src/news_agent/storage/db.py 的 _SCHEMA）保持一致，
-- tests/test_submission_files.py 会校验两者不漂移。
-- 说明：应用启动时会自动执行同样的建表语句；本文件供代码评审与独立建库使用。

CREATE TABLE IF NOT EXISTS profile (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    name       TEXT NOT NULL,
    email      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    topics_json           TEXT NOT NULL,
    keywords_json         TEXT NOT NULL,
    exclude_keywords_json TEXT NOT NULL,
    max_items             INTEGER NOT NULL,
    updated_at            TEXT NOT NULL
);

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
);

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
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_date ON agent_runs(date);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
