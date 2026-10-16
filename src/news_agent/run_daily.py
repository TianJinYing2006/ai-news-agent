"""每日运行编排：CLI / 定时任务的唯一入口（docs §3.5）。

流程：检查必要配置与 profile → 原子 claim → `generate` 或 `retry_send` →
（仅 generate）Agent → 校验 finish 产物 → 归档 → 标记 sending → SMTP → 状态流转。

职责边界：

- **不可撤销副作用只在这里**：发邮件、状态落库；工具层与模型都碰不到；
- 失败一律 `mark_failed` + 保存错误与 trace，**不产生降级简报**；
- `dry_run` 用 `<date>-dry-run` 独立工作区走真实提示词 / Agent / 校验与渲染，
  但产物只写临时目录、不发信、不写任何正式状态；
- 异常兜底：只要本进程持有认领（generate/retry_send），任何未捕获异常都会
  写 `failed`，不留永久占用的运行状态。
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .agent.client import build_client
from .agent.loop import run_agent
from .agent.prompt import build_prompts
from .briefing import archive_briefing, check_briefing, render_document_html
from .config import (
    BRIEFINGS_DIR,
    DATA_DIR,
    WORKSPACE_DIR,
    Config,
    LLMConfig,
    ensure_dirs,
)
from .delivery.email import SendResult, send_briefing
from .storage import (
    CLAIM_ALREADY_RUNNING,
    CLAIM_GENERATE,
    CLAIM_NO_ARTIFACT,
    CLAIM_RETRY_SEND,
    CLAIM_SKIPPED,
    Database,
    claim_daily_run,
    get_briefing,
    get_profile,
    get_subscription,
    init_db,
    insert_run,
    mark_failed,
    mark_generated,
    mark_sending,
    mark_sent,
)
from .tools import builtin
from .tools.registry import ToolRegistry

TZ = ZoneInfo("Asia/Shanghai")
LOG_DIR = DATA_DIR / "logs"
SUCCESS_STATUSES = {"sent", "skipped", "dry_run"}

logger = logging.getLogger("news_agent.run_daily")


@dataclass(frozen=True)
class RunOutcome:
    status: str  # sent | skipped | already_running | dry_run | failed
    date: str
    briefing_path: Path | None
    message: str


@dataclass
class RunDeps:
    """装配点：默认真实实现；测试注入假 client / 假发送函数 / 临时目录。"""

    config: Config
    db: Database
    client_factory: Callable[[LLMConfig], Any]
    sender: Callable[..., SendResult]
    registry: ToolRegistry
    briefings_dir: Path
    workspace_dir: Path

    @classmethod
    def default(cls) -> RunDeps:
        ensure_dirs()
        return cls(
            config=Config.load(),
            db=Database(),
            client_factory=build_client,
            sender=send_briefing,
            registry=builtin.build_registry(),
            briefings_dir=BRIEFINGS_DIR,
            workspace_dir=WORKSPACE_DIR,
        )


def run_daily(
    date: str | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    resend: bool = False,
    deps: RunDeps | None = None,
) -> RunOutcome:
    """执行一次每日运行。CLI、定时任务与手动触发共用此入口。

    `force`（强制重跑）与 `resend`（仅重发）是显式的人工越权入口，
    只跳过"已发送"防重；**并发保护（约租期内不可越过）依然生效**。
    """
    deps = deps or RunDeps.default()
    date = date or _today()
    init_db(deps.db)

    claimed = {"value": False}

    def execute() -> RunOutcome:
        if force and resend:
            return RunOutcome("failed", date, None, "force 与 resend 不能同时使用")
        if dry_run and (force or resend):
            return RunOutcome("failed", date, None, "dry-run 不与 force/resend 组合使用")
        if dry_run:
            return _dry_run(date, deps)

        cfg = deps.config
        if not cfg.llm.api_key:
            return RunOutcome(
                "failed", date, None, "未配置 DEEPSEEK_API_KEY：真实生成需要模型（见 .env）"
            )
        profile = get_profile(deps.db)
        if profile is None or not (profile.get("email") or "").strip():
            return RunOutcome(
                "failed", date, None, "尚未配置收件身份：请先在网页「身份」页填写姓名与邮箱"
            )

        action = claim_daily_run(deps.db, date, force=force, resend=resend)
        logger.info("claim=%s date=%s force=%s resend=%s", action, date, force, resend)
        claimed["value"] = action in (CLAIM_GENERATE, CLAIM_RETRY_SEND)

        if action == CLAIM_SKIPPED:
            return RunOutcome(
                "skipped", date, _existing_archive(deps, date), "当天简报已发送，跳过"
            )
        if action == CLAIM_ALREADY_RUNNING:
            return RunOutcome("already_running", date, None, "租约内已有运行在跑，跳过本次触发")
        if action == CLAIM_NO_ARTIFACT:
            return RunOutcome(
                "skipped",
                date,
                None,
                "当天没有可重发的归档简报（尚未生成或产物缺失）；如需生成请用普通运行或强制重跑",
            )
        if action == CLAIM_RETRY_SEND:
            return _send_existing(date, deps, profile)
        return _generate_then_send(date, deps, profile, cfg)

    try:
        outcome = execute()
    except Exception as e:
        message = f"运行异常：{type(e).__name__}: {e}"
        logger.exception("run_daily 未捕获异常 date=%s", date)
        if claimed["value"]:
            try:
                mark_failed(deps.db, date, "pipeline", message)
            except Exception:
                logger.exception("mark_failed 也失败了 date=%s", date)
        return RunOutcome("failed", date, None, message)

    logger.info("outcome status=%s date=%s message=%s", outcome.status, date, outcome.message)
    return outcome


# ---------------------------------------------------------------------------
# 生成路径
# ---------------------------------------------------------------------------


def _generate_then_send(date: str, deps: RunDeps, profile: dict, cfg: Config) -> RunOutcome:
    workspace = deps.workspace_dir / date
    workspace.mkdir(parents=True, exist_ok=True)
    briefing_path = workspace / "briefing.md"
    subscription = get_subscription(deps.db)

    try:
        client = deps.client_factory(cfg.llm)
    except Exception as e:  # noqa: BLE001
        message = f"初始化模型客户端失败：{type(e).__name__}: {e}"
        mark_failed(deps.db, date, "model", message)
        return RunOutcome("failed", date, None, message)

    system_prompt, user_prompt = build_prompts(
        subscription, date=date, briefing_path=str(briefing_path)
    )
    started = _now_iso()
    result = run_agent(
        client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        tools_schema=deps.registry.schemas(),
        registry=deps.registry,
        model=cfg.llm.model,
        max_steps=cfg.llm.max_steps,
        token_budget=cfg.llm.token_budget,
        loop_window=cfg.llm.loop_window,
    )
    insert_run(
        deps.db,
        date,
        started,
        _now_iso(),
        result.status,
        result.trace,
        result.error,
        steps=result.steps,
        tool_calls=result.tool_call_count,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
    )
    logger.info(
        "agent status=%s steps=%d tools=%d tokens=%d/%d",
        result.status,
        result.steps,
        result.tool_call_count,
        result.tokens_in,
        result.tokens_out,
    )

    if result.status != "finished" or result.briefing_path is None:
        message = f"Agent 未产出合格简报：{result.error or result.status}"
        mark_failed(deps.db, date, "model", message)
        return RunOutcome("failed", date, None, message)

    check = check_briefing(
        result.briefing_path, workspace_root=workspace, max_items=subscription["max_items"]
    )
    if not check.ok:
        message = f"简报校验失败：{check.error}"
        mark_failed(deps.db, date, "validation", message)
        return RunOutcome("failed", date, None, message)

    md_text = Path(result.briefing_path).read_text(encoding="utf-8", errors="replace")
    summary = result.summary or f"每日 AI 简报（{date}）：{check.item_count} 条"
    md_path, html_path = archive_briefing(
        md_text,
        date=date,
        title=check.title or f"每日 AI 简报（{date}）",
        briefings_dir=deps.briefings_dir,
    )
    mark_generated(
        deps.db,
        date,
        md_path=str(md_path),
        html_path=str(html_path),
        summary=summary,
        item_count=check.item_count,
    )
    mark_sending(deps.db, date)
    return _deliver(
        date, deps, profile["email"], summary=summary, md_path=md_path, html_path=html_path
    )


def _send_existing(date: str, deps: RunDeps, profile: dict) -> RunOutcome:
    """SMTP-only 重试：重校验已有产物后只重发邮件，绝不重跑 LLM。"""
    md_path = deps.briefings_dir / f"{date}.md"
    html_path = deps.briefings_dir / f"{date}.html"
    subscription = get_subscription(deps.db)

    check = check_briefing(
        md_path, workspace_root=deps.briefings_dir, max_items=subscription["max_items"]
    )
    if not check.ok:
        message = f"复用产物校验失败：{check.error}"
        mark_failed(deps.db, date, "validation", message)
        return RunOutcome("failed", date, md_path if md_path.exists() else None, message)

    row = get_briefing(deps.db, date)
    summary = (row or {}).get("summary") or f"每日 AI 简报（{date}）"
    return _deliver(date, deps, profile["email"], summary=summary, md_path=md_path, html_path=html_path)


def _deliver(
    date: str,
    deps: RunDeps,
    to_email: str,
    *,
    summary: str,
    md_path: Path,
    html_path: Path,
) -> RunOutcome:
    if html_path.is_file():
        html_body = html_path.read_text(encoding="utf-8", errors="replace")
    else:
        md_text = md_path.read_text(encoding="utf-8", errors="replace")
        html_body = render_document_html(md_text, title=f"每日 AI 简报（{date}）")

    result = deps.sender(
        deps.config.smtp,
        to_email=to_email,
        subject=f"每日 AI 简报 · {date}",
        text_body=summary,
        html_body=html_body,
    )
    if result.ok:
        mark_sent(deps.db, date)
        return RunOutcome("sent", date, md_path, f"已发送（第 {result.attempts} 次尝试）")

    message = f"邮件发送失败：{result.error or '未知错误'}"
    mark_failed(deps.db, date, "smtp", message)
    return RunOutcome("failed", date, md_path, message)


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def _dry_run(date: str, deps: RunDeps) -> RunOutcome:
    cfg = deps.config
    if not cfg.llm.api_key:
        return RunOutcome("failed", date, None, "未配置 DEEPSEEK_API_KEY：dry-run 同样要真实跑模型")

    subscription = get_subscription(deps.db)
    workspace = deps.workspace_dir / f"{date}-dry-run"
    workspace.mkdir(parents=True, exist_ok=True)
    briefing_path = workspace / "briefing.md"

    try:
        client = deps.client_factory(cfg.llm)
    except Exception as e:  # noqa: BLE001
        return RunOutcome("failed", date, None, f"初始化模型客户端失败：{type(e).__name__}: {e}")

    system_prompt, user_prompt = build_prompts(
        subscription, date=date, briefing_path=str(briefing_path)
    )
    result = run_agent(
        client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        tools_schema=deps.registry.schemas(),
        registry=deps.registry,
        model=cfg.llm.model,
        max_steps=cfg.llm.max_steps,
        token_budget=cfg.llm.token_budget,
        loop_window=cfg.llm.loop_window,
    )
    if result.status != "finished" or result.briefing_path is None:
        return RunOutcome(
            "failed", date, None, f"dry-run：Agent 未产出简报（{result.error or result.status}）"
        )

    check = check_briefing(
        result.briefing_path, workspace_root=workspace, max_items=subscription["max_items"]
    )
    if not check.ok:
        return RunOutcome("failed", date, None, f"dry-run：简报校验失败：{check.error}")

    md_text = Path(result.briefing_path).read_text(encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory(prefix="news_agent_dryrun_") as tmp:
        archive_briefing(
            md_text,
            date=date,
            title=check.title or f"每日 AI 简报（{date}）",
            briefings_dir=tmp,
        )

    return RunOutcome(
        "dry_run",
        date,
        Path(result.briefing_path),
        f"dry-run 完成：{check.item_count} 条（模型步数 {result.steps}）；未发信、未改正式状态",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="news_agent.run_daily", description="每日 AI 新闻简报（本机单用户）"
    )
    parser.add_argument("--date", help="业务日期 YYYY-MM-DD，默认 Asia/Shanghai 今天")
    parser.add_argument(
        "--dry-run", action="store_true", help="独立工作区跑通链路：不发信、不改正式状态"
    )
    override = parser.add_mutually_exclusive_group()
    override.add_argument(
        "--force",
        action="store_true",
        help="强制重跑：忽略当天已发送状态，重新生成并再发一封（覆盖当天归档）",
    )
    override.add_argument(
        "--resend",
        action="store_true",
        help="仅重发：不调模型，把当天已归档的简报再发一封邮件",
    )
    args = parser.parse_args(argv)

    setup_file_logging()
    outcome = run_daily(args.date, dry_run=args.dry_run, force=args.force, resend=args.resend)
    line = f"[{outcome.status}] {outcome.date}：{outcome.message}"
    print(line)
    logger.info(line)
    return 0 if outcome.status in SUCCESS_STATUSES else 1


def setup_file_logging() -> None:
    """轮转文件日志（`data/logs/news_agent.log`）。CLI 与 web 进程共用。"""
    logger.setLevel(logging.INFO)
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / "news_agent.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def _existing_archive(deps: RunDeps, date: str) -> Path | None:
    md_path = deps.briefings_dir / f"{date}.md"
    return md_path if md_path.is_file() else None


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


if __name__ == "__main__":
    sys.exit(main())
