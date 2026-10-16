# AGENTS.md —— 项目约束与协作规则

每日 AI 新闻助手（news_agent）。权威文档：`docs/implementation-plan.md`（以此为准），技术论证：`docs/tech-stack-selection.md`。

## 协作工作流：一需求 = 一分支 = 一 PR = CLI 端校验

- `main`：稳定分支，始终保持可运行；只接受来自 `dev` 的合并。
- `dev`：日常开发集成分支。所有需求分支从 `dev` 切出，PR 合回 `dev`。
- 分支命名：`feat/`、`fix/`、`docs/`、`chore/` + 简短范围，如 `feat/storage-claim`。
- 分支内只做本需求相关改动，不夹带无关重构。
- PR 前必须跑 CLI 校验且全绿；PR 用 `gh` CLI 创建，目标为 `dev`。
- PR 描述必须写清：需求、变更点、校验命令与结果、未验证/未完成事项。
- 不直接向 `main`/`dev` 提交改动（仓库初始化除外）。

CLI 校验命令（PR 前必跑，全绿才可提 PR）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

涉及运行链路时补跑 dry-run（临时工作区，不发信、不改正式状态）：

```powershell
.\.venv\Scripts\python.exe -m news_agent.run_daily --date 2026-10-14 --dry-run
```

## 硬性设计约束

1. **工具调用顺序必须真的由 LLM 决定**：提示词与 pipeline 不得写死步骤顺序；验收看受控测试（假 client）与 `agent_runs.trace_json`，不靠“连跑几天看轨迹”。
2. **不可撤销副作用不进工具层**：邮件发送、数据库写入属于编排层；Agent 最多做到写文件 + `finish`，`send_email` 不在工具列表。
3. **三道硬闸**：`max_steps=30`、`token_budget=800000`、连续重复调用检测；任一触发即失败并记录，**不发降级/编造简报**。
4. **只有校验通过的简报才发送**；失败保留错误与 trace，网页可见。
5. **MVP 明确不做**（勿加回）：Langfuse/OTel、双调度器并存（调度只保留 APScheduler 进程内一种）、独立 trace 页、跨日去重表、分页 API、WebSocket、公网鉴权/多用户登录。（「强制重跑 / 重发邮件」是用户显式发起的受控例外：网页带二次确认、CLI 用 `--force` / `--resend`；**运行进行中不可被越过**，详见实施计划 §3.2。）
6. **凭据只在本机环境变量**（Windows 用户级变量优先；项目 `.env` 为可选兜底且已 gitignore）：不得写入代码、SQLite、trace、日志、文档或提交。
7. 日期口径 `Asia/Shanghai`；网页只监听 `127.0.0.1`。
8. 破坏性/外部操作（删除、发信、force push、改远程状态）先说明再执行；需求梳理阶段只改文档不写代码。

## 环境事实（本机实测）

- Windows + PowerShell 5.1；venv 在 `.venv/`，`pip install -e ".[dev]"` 安装；运行入口 `python -m news_agent.run_daily`。
- **联网一律直连**（`trust_env=False`）：本机代理对所有目标站点不通；抓源必须带 User-Agent。
- DeepSeek `deepseek-flash`：thinking 模式带 tools 时必须回传 `reasoning_content`，不要手工重建 assistant 消息。
- 国外 RSS 源不可达；源清单见 `src/news_agent/sources/feeds.py`（9 个国内源实测可用）。
- 凭据状态：已用 Windows 用户级环境变量配置（DeepSeek / SMTP / Tavily，2026-10-14）；真实联调记录：tool-calling 两轮自检、SMTP 登录、dry-run（10 条/14 步）、正式投递（10 条/17 步/33 次工具调用，1 次成功）。
- PyPI 直连慢，可临时用 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

## 关键路径

- 契约/排期：`docs/implementation-plan.md`
- 代码：`src/news_agent/`（tools/ sources/ storage/ agent/ delivery/ web/）
- 测试：`tests/`；简报归档：`data/briefings/YYYY-MM-DD.md|.html`
