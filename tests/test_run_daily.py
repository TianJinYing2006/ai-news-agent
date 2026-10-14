"""pipeline 端到端（全 mock）：成功、跳过、租约内并发、失败路径、SMTP 重发、dry-run。

对应实施计划阶段 5 验收：mock Agent 与 SMTP 验证成功、重复运行跳过、生成失败不发信、
邮件失败仍保留网页简报；**SMTP 失败重跑只复用简报、不再次调用 Agent**；
dry-run 不发信、不改正式状态。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from news_agent import run_daily as module
from news_agent.config import Config, LLMConfig, SMTPConfig
from news_agent.delivery.email import SendResult
from news_agent.run_daily import RunDeps, RunOutcome, run_daily
from news_agent.storage import (
    Database,
    claim_daily_run,
    get_briefing,
    init_db,
    save_profile,
)
from news_agent.tools.control import finish as finish_tool
from news_agent.tools.registry import Tool, ToolRegistry

DATE = "2026-10-14"

VALID_MD = """# 每日 AI 简报（2026-10-14）

## 某模型发布新版本

- 来源：量子位 · 2026-10-14
- 摘要：模型能力提升。
- 原文：https://example.com/news/1
"""

INVALID_MD = "# 每日 AI 简报（2026-10-14）\n\n## 没有链接的条目\n\n摘要：foo\n"


class FakeClient:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, model: str, messages: list[dict], tools: list[dict]) -> SimpleNamespace:
        self.requests.append([dict(m) for m in messages])
        idx = min(len(self.requests) - 1, len(self.script) - 1)
        item = self.script[idx]
        return item(messages) if callable(item) else item


class BoomClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> SimpleNamespace:
        raise RuntimeError("API 崩了")


def _finish(path: Path, summary: str = "今日简报") -> SimpleNamespace:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "finish",
                    "arguments": json.dumps(
                        {"summary": summary, "briefing_path": str(path)}, ensure_ascii=False
                    ),
                },
            }
        ],
    }
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _client_writing(path: Path, md_text: str, summary: str = "今日简报") -> FakeClient:
    def respond(messages: list[dict]) -> SimpleNamespace:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md_text, encoding="utf-8")
        return _finish(path, summary=summary)

    return FakeClient([respond])


def _content_only(text: str) -> FakeClient:
    message = {"role": "assistant", "content": text}
    return FakeClient([SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)])


class FakeSender:
    def __init__(self, results: list[SendResult]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(
        self, cfg: SMTPConfig, *, to_email: str, subject: str, text_body: str, html_body: str
    ) -> SendResult:
        self.calls.append(
            {
                "to_email": to_email,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
            }
        )
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="finish",
            description="宣告完成",
            parameters={"type": "object", "properties": {}},
            fn=finish_tool,
        )
    )
    return registry


def _config(api_key: str = "test-key") -> Config:
    return Config(
        llm=LLMConfig(
            api_key=api_key,
            base_url="http://unused.local",
            model="deepseek-flash",
            thinking_enabled=True,
            max_steps=30,
            token_budget=800_000,
            loop_window=3,
        ),
        tavily_api_key="",
        smtp=SMTPConfig(
            host="smtp.test",
            port=465,
            user="sender@test.com",
            password="x",
            from_name="News Agent",
        ),
    )


def _deps(
    tmp_path: Path,
    *,
    client: Any = None,
    sender: FakeSender | None = None,
    api_key: str = "test-key",
) -> RunDeps:
    briefings = tmp_path / "briefings"
    workspace = tmp_path / "workspace"
    briefings.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    db = Database(tmp_path / "test.db", briefings_dir=briefings)
    init_db(db)
    return RunDeps(
        config=_config(api_key=api_key),
        db=db,
        client_factory=lambda cfg: client,
        sender=sender or FakeSender([SendResult(ok=True, attempts=1)]),
        registry=_registry(),
        briefings_dir=briefings,
        workspace_dir=workspace,
    )


def _seed_profile(deps: RunDeps) -> None:
    save_profile(deps.db, "测试用户", "reader@test.com")


# ---------- 成功与跳过 ----------


def test_success_flow(tmp_path: Path) -> None:
    client = _client_writing(tmp_path / "workspace" / DATE / "briefing.md", VALID_MD)
    deps = _deps(tmp_path, client=client)
    _seed_profile(deps)

    outcome = run_daily(DATE, deps=deps)

    assert outcome.status == "sent"
    row = get_briefing(deps.db, DATE)
    assert row is not None
    assert row["status"] == "sent" and row["sent_at"]
    assert row["item_count"] == 1
    assert (deps.briefings_dir / f"{DATE}.md").is_file()
    assert (deps.briefings_dir / f"{DATE}.html").is_file()

    runs = deps.db.fetchall("SELECT * FROM agent_runs")
    assert len(runs) == 1 and runs[0]["status"] == "finished"

    assert len(deps.sender.calls) == 1
    call = deps.sender.calls[0]
    assert call["to_email"] == "reader@test.com"
    assert DATE in call["subject"]
    assert call["html_body"].startswith("<!DOCTYPE html>")


def test_sent_is_skipped_on_second_run(tmp_path: Path) -> None:
    client = _client_writing(tmp_path / "workspace" / DATE / "briefing.md", VALID_MD)
    deps = _deps(tmp_path, client=client)
    _seed_profile(deps)

    assert run_daily(DATE, deps=deps).status == "sent"
    second = run_daily(DATE, deps=deps)

    assert second.status == "skipped"
    assert len(client.requests) == 1  # 没有第二次调用模型
    assert len(deps.sender.calls) == 1


def test_already_running_within_lease(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    _seed_profile(deps)
    claim_daily_run(deps.db, DATE)  # 制造新鲜的 generating 行

    outcome = run_daily(DATE, deps=deps)

    assert outcome.status == "already_running"
    assert deps.sender.calls == []


# ---------- 配置与失败路径 ----------


def test_missing_key_fails_without_claim(tmp_path: Path) -> None:
    deps = _deps(tmp_path, api_key="")
    _seed_profile(deps)

    outcome = run_daily(DATE, deps=deps)

    assert outcome.status == "failed"
    assert "DEEPSEEK_API_KEY" in outcome.message
    assert get_briefing(deps.db, DATE) is None  # 不占用当天状态


def test_missing_profile_fails_without_claim(tmp_path: Path) -> None:
    deps = _deps(tmp_path)

    outcome = run_daily(DATE, deps=deps)

    assert outcome.status == "failed"
    assert "身份" in outcome.message
    assert get_briefing(deps.db, DATE) is None


def test_agent_incomplete_marks_failed_no_email(tmp_path: Path) -> None:
    deps = _deps(tmp_path, client=_content_only("我搞不定"))
    _seed_profile(deps)

    outcome = run_daily(DATE, deps=deps)

    assert outcome.status == "failed"
    row = get_briefing(deps.db, DATE)
    assert row is not None and row["status"] == "failed" and row["failure_stage"] == "model"
    assert deps.sender.calls == []
    runs = deps.db.fetchall("SELECT * FROM agent_runs")
    assert len(runs) == 1 and runs[0]["status"] == "incomplete"


def test_validation_failure_no_email(tmp_path: Path) -> None:
    client = _client_writing(tmp_path / "workspace" / DATE / "briefing.md", INVALID_MD)
    deps = _deps(tmp_path, client=client)
    _seed_profile(deps)

    outcome = run_daily(DATE, deps=deps)

    assert outcome.status == "failed"
    assert "校验" in outcome.message
    row = get_briefing(deps.db, DATE)
    assert row is not None and row["failure_stage"] == "validation"
    assert deps.sender.calls == []
    assert not (deps.briefings_dir / f"{DATE}.md").exists()  # 不合格产物不归档


def test_finish_outside_workspace_rejected(tmp_path: Path) -> None:
    client = _client_writing(tmp_path / "outside.md", VALID_MD)
    deps = _deps(tmp_path, client=client)
    _seed_profile(deps)

    outcome = run_daily(DATE, deps=deps)

    assert outcome.status == "failed"
    assert "工作区" in outcome.message
    assert deps.sender.calls == []


def test_agent_error_status_recorded(tmp_path: Path) -> None:
    deps = _deps(tmp_path, client=BoomClient())
    _seed_profile(deps)

    outcome = run_daily(DATE, deps=deps)

    assert outcome.status == "failed"
    row = get_briefing(deps.db, DATE)
    assert row is not None and row["failure_stage"] == "model"
    runs = deps.db.fetchall("SELECT * FROM agent_runs")
    assert len(runs) == 1 and runs[0]["status"] == "error"


# ---------- SMTP 失败与重发 ----------


def test_smtp_failure_keeps_archived_briefing(tmp_path: Path) -> None:
    sender = FakeSender([SendResult(ok=False, attempts=3, error="SMTP 连接超时")])
    client = _client_writing(tmp_path / "workspace" / DATE / "briefing.md", VALID_MD)
    deps = _deps(tmp_path, client=client, sender=sender)
    _seed_profile(deps)

    outcome = run_daily(DATE, deps=deps)

    assert outcome.status == "failed"
    row = get_briefing(deps.db, DATE)
    assert row is not None and row["failure_stage"] == "smtp"
    assert (deps.briefings_dir / f"{DATE}.md").is_file()  # 网页仍可查看
    assert (deps.briefings_dir / f"{DATE}.html").is_file()
    assert len(sender.calls) == 1


def test_smtp_retry_reuses_artifact_without_agent(tmp_path: Path) -> None:
    sender = FakeSender(
        [
            SendResult(ok=False, attempts=3, error="超时"),
            SendResult(ok=True, attempts=1),
        ]
    )
    client = _client_writing(tmp_path / "workspace" / DATE / "briefing.md", VALID_MD)
    deps = _deps(tmp_path, client=client, sender=sender)
    _seed_profile(deps)

    first = run_daily(DATE, deps=deps)
    assert first.status == "failed"

    second = run_daily(DATE, deps=deps)
    assert second.status == "sent"
    assert len(client.requests) == 1  # 关键验收：只复用产物，没有再次调用模型
    assert len(sender.calls) == 2
    row = get_briefing(deps.db, DATE)
    assert row is not None and row["status"] == "sent"


# ---------- dry-run ----------


def test_dry_run_success_no_side_effects(tmp_path: Path) -> None:
    dry_ws = tmp_path / "workspace" / f"{DATE}-dry-run"
    client = _client_writing(dry_ws / "briefing.md", VALID_MD)
    deps = _deps(tmp_path, client=client)

    outcome = run_daily(DATE, dry_run=True, deps=deps)

    assert outcome.status == "dry_run"
    assert outcome.briefing_path is not None and outcome.briefing_path.is_file()
    assert get_briefing(deps.db, DATE) is None  # 无状态行
    assert deps.sender.calls == []
    assert not (deps.briefings_dir / f"{DATE}.md").exists()  # 未落正式归档
    assert deps.db.fetchall("SELECT * FROM agent_runs") == []


def test_dry_run_requires_key(tmp_path: Path) -> None:
    deps = _deps(tmp_path, api_key="")

    outcome = run_daily(DATE, dry_run=True, deps=deps)

    assert outcome.status == "failed"
    assert "DEEPSEEK_API_KEY" in outcome.message


# ---------- CLI ----------


def test_main_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_setup_file_logging", lambda: None)

    monkeypatch.setattr(
        module, "run_daily", lambda date=None, dry_run=False: RunOutcome("sent", DATE, None, "ok")
    )
    assert module.main(["--date", DATE]) == 0

    monkeypatch.setattr(
        module,
        "run_daily",
        lambda date=None, dry_run=False: RunOutcome("failed", DATE, None, "boom"),
    )
    assert module.main(["--date", DATE]) == 1

    captured: dict[str, Any] = {}

    def fake(date: str | None = None, dry_run: bool = False) -> RunOutcome:
        captured["dry_run"] = dry_run
        return RunOutcome("dry_run", DATE, None, "ok")

    monkeypatch.setattr(module, "run_daily", fake)
    assert module.main(["--dry-run"]) == 0
    assert captured["dry_run"] is True
