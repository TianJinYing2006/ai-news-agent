"""本机入口：`python -m news_agent.web`（只监听 127.0.0.1，含进程内调度）。"""

from __future__ import annotations

import uvicorn

from ..run_daily import setup_file_logging
from .app import create_app


def main() -> None:
    setup_file_logging()
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
