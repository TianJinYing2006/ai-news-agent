"""交付物文件一致性测试：

- `sql/schema.sql` 必须与 `news_agent.storage.db._SCHEMA` 不漂移；
- `requirements.txt` 的直接依赖必须与 `pyproject.toml` 的 dependencies 一致。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from news_agent.storage.db import _SCHEMA

ROOT = Path(__file__).resolve().parents[1]


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().rstrip(";")


def test_schema_sql_matches_code() -> None:
    raw = (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
    without_comments = "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("--")
    )
    statements = [stmt for stmt in without_comments.split(";") if stmt.strip()]
    assert {_normalize(stmt) for stmt in statements} == {_normalize(stmt) for stmt in _SCHEMA}


def test_requirements_txt_matches_pyproject() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {dep.strip() for dep in pyproject["project"]["dependencies"]}

    listed: set[str] = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        listed.add(line)

    assert listed == declared
