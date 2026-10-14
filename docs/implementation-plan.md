# 每日 AI 新闻助手 —— 实施计划 v1.2

日期：2026-10-14  
依据：用户需求；技术选择见 `docs/tech-stack-selection.md`

## 1. 目标与范围

实现一个本机运行的每日 AI 新闻简报工具：用户在网页填写身份和订阅偏好；每日任务运行时读取偏好；LLM 通过 function calling 自己选择工具、顺序和调用次数；程序将有效简报存档并通过邮件发送。

### MVP 边界

- 单机、单用户、单份订阅配置；不做注册登录、多租户或公网部署。网页默认只监听 `127.0.0.1`。
- Python + FastAPI/Jinja2 + SQLite；简报 Markdown/HTML 文件落在 `data/briefings/`。
- 手写 ReAct 循环。保留需求指定的五个工具，并加入 RSS、网页搜索、正文抓取和结束工具。网络工具给模型能力，不规定它必须按固定顺序调用。
- 邮件作为推送渠道。邮件发送由 pipeline 在简报通过校验后执行，模型不能自行发送。
- trace 保存在 SQLite，供排障和验证循环行为；MVP 不接 Langfuse/OTel，不做独立运行轨迹页面。
- 调度采用进程内 APScheduler（随 FastAPI 启动，每日 07:00 `Asia/Shanghai`，时间经 `DAILY_RUN_AT` 配置）：单进程单 worker、任务不重叠（`max_instances=1`、`MemoryJobStore` 不引入持久化任务库）；不引入第二调度器（含系统任务计划程序）。服务未运行期间错过的任务不自动补跑。
- 日期按 `Asia/Shanghai` 计算，调度目标为北京时间早上 07:00；日志写入本机轮转文件并在失败时返回非零退出码，便于调度运行与手动排查。

不承诺任一外部服务故障时仍发送一封内容不可靠的“降级简报”。失败时保留错误和 trace，在网页显示状态；有效简报仍可查看。只有校验通过的简报才发送。

### 设计取舍

| 决策 | 为什么适合当前需求 | 暂不选的方案 |
|---|---|---|
| Python | 现有工具、RSS 解析和正文抽取库成熟，能直接复用已完成的工具层；项目重心是 Agent 行为，不是服务端吞吐 | Java 也能实现，但在本项目没有既有 Java 代码或团队约束，改用 Java 只会增加 RSS/抽取实现成本 |
| 手写 ReAct + OpenAI 兼容 SDK | 核心验收点就是 LLM 看见工具结果后再决定下一步；单循环可直接检查消息历史、停止条件和错误回灌 | LangGraph/LangChain 的状态图、持久化和多 Agent 能力当前用不到；不是不能用，而是没有需求抵消其额外抽象 |
| RSS + Tavily 搜索 | RSS 提供低成本稳定候选源，Tavily 给模型按需补搜的能力；来源组合和调用次数仍由模型选择 | 纯搜索增加额度依赖且覆盖不稳定；固定抓完所有 RSS 再总结会把流程顺序交给程序，削弱核心要求 |
| SQLite + 文件归档 | 单机单用户、每天一份，SQLite 足够保存设置、状态和 trace；Markdown/HTML 文件便于网页、邮件和现有文件工具读取 | PostgreSQL 需要单独服务；把全文只放数据库会让文件工具无法检索，数据库也没有带来当前需要的查询能力 |
| FastAPI + Jinja2 | 身份、订阅、历史是简单表单和阅读页，服务端模板即可完成并直接复用 Python 数据层 | React/Vue 会增加构建、路由和 API 维护工作，当前没有复杂交互来支撑这份成本 |
| SMTP | 对每天发一封邮件，SMTP 配置量小、可由进程内调度无人值守调用 | Agent Mail 等需要逐封确认的通道不适合无人值守；暂不引入邮件服务商 API，因为需求没有模板、投递分析或批量发送要求 |
| 受限 `bash(command)` | 保留需求要求的命令工具，用现有白名单支持常见目录/文本检查；当前命令由 Python 在受限路径内实现，跨平台且不启动任意 shell | `shell=True` 或任意子进程可让模型运行删除、下载、持久化等任意命令；以后若改用 `subprocess`，仍应 `shell=False`、白名单和超时 |
| APScheduler（随 FastAPI 启动） | 交付与演示只有一个启动入口——web 服务自带调度，免操作系统配置；任务复用统一 `run_daily()`；单实例不重叠 + SQLite 原子认领双重防重 | Windows 任务计划程序：逐机手工配置、演示不便；双调度器：重复触发风险。错过的自动触发不补跑（README 说明） |
| SQLite 精简 trace | 一份本地记录足以查看工具选择和失败原因，也能支撑测试 | Langfuse/OTel 对单机单用户增加服务/配置/敏感数据流转；出现远程团队协作或线上监控需求后再评估 |
| 本机单用户，不做登录 | 当前需求是一个用户设置自己的订阅，且页面默认只在本机开放；不引入账号系统即可完成目标 | 多用户和公网部署需要认证、授权、隔离及账户恢复，当前没有对应需求，做了反而扩大安全面 |

这些选择不是对未来的限制。如果后续需要多人共享、断点续跑、线上告警或高可用，再依据实际问题引入对应方案。

### 完整技术选型清单

| 层 | 选型 | 用途与理由 | 当前不选 |
|---|---|---|---|
| 语言与运行时 | Python 3.13；包声明兼容 Python >=3.11 | 现有工具层与生态可复用；3.13 是开发/部署基准，兼容声明保留安装弹性 | Java：没有现存 Java 代码或部署约束，迁移会重做工具层 |
| 包与环境 | `pyproject.toml`、Hatchling、项目 `.venv`、`python-dotenv` | 标准 Python 包安装和隔离环境；`.env` 仅供本机配置 | Conda：本项目依赖不需要其环境管理能力 |
| Agent 编排 | 手写 ReAct / function-calling 循环，OpenAI Python SDK | 直接处理模型消息、工具调用、结果回传和停止条件，便于验证模型决定工具顺序 | LangGraph/LangChain：能力足够但当前不用状态图、断点恢复或多 Agent；并非不满足需求 |
| 模型 | DeepSeek `deepseek-flash`，OpenAI 兼容 API | 已有原生 tool calling，能复用 SDK；配置保留 base URL 和模型名 | 为单模型 MVP 再引入多模型路由或 LangChain 模型适配层 |
| 新闻获取 | `feedparser` 读取 RSS/Atom；Tavily Python SDK 按需搜索；`httpx` 访问网页；`trafilatura` 抽正文 | RSS 无 key、适合作主源；Tavily 补充模型自主搜索；分层工具可让模型按需选择来源 | 纯搜索：额度和覆盖不稳定；固定抓取全部源：会把流程顺序写死 |
| 工具安全 | 项目内 `ToolRegistry`；文件路径限制；受限 `bash` 白名单；HTTP 超时、SSRF 地址/跳转复查、响应体上限 | 工具执行仍由模型选择，但参数与副作用由程序设边界；bash 不启动任意 shell | 任意 shell、开放式 URL 抓取或把发邮件暴露给模型 |
| 数据存储 | SQLite（WAL、`busy_timeout`、`BEGIN IMMEDIATE`） | 单机单用户、低写入量，零服务运维；原子认领支持并发防重和陈旧租约回收 | PostgreSQL：需要单独服务，当前数据规模和部署不需要 |
| 简报归档 | `data/briefings/YYYY-MM-DD.md` + `.html`；SQLite 只存状态、索引和精简 trace | Markdown 易检索和审阅，HTML 可直接用于网页/邮件；避免数据库正文与文件工具两套内容 | 只存数据库：文件工具无法直接检索；单独文章库：跨日去重不在 MVP 范围 |
| HTML 安全 | `markdown-it-py`（禁原始 HTML）+ `nh3` allowlist | 将模型生成的 Markdown 安全渲染为网页和邮件 HTML | 自行用正则写 sanitizer：难以完整处理不可信 HTML |
| Web | FastAPI、Jinja2、原生 HTML 表单和少量 JavaScript、FastAPI TestClient | 三类简单页面共用 Python 服务和数据层，无前端构建链；仅监听 `127.0.0.1` | React/Vue：增加构建、API 状态和部署维护，但交互复杂度不足以抵消成本 |
| 推送 | Python `smtplib`，SSL/STARTTLS；SMTP 授权码来自环境变量，收件人来自 profile | 每日单封可无人值守发送；生成/校验与外部发送副作用分离 | 需人工确认的 Agent Mail；当前不需要邮件服务商 API 的模板/统计能力 |
| 调度 | APScheduler（进程内，随 FastAPI 启动）：每日 Asia/Shanghai 07:00（`DAILY_RUN_AT` 可配），调用统一 `run_daily()` | 单入口交付（启动 web 即含调度），免操作系统配置；`max_instances=1` + `coalesce` 不重叠，原子认领兜底 | Windows 任务计划程序：逐机配置、演示成本高；持久化任务库 / 第二调度器：当前不需要 |
| 测试与质量 | pytest、Ruff；mock LLM/SMTP；FastAPI TestClient | 离线覆盖确定性分支，受控测试直接证明模型结果影响下一步工具选择 | 把真实模型连续多天行为当作唯一验收；引入端到端测试平台 |
| 日志与配置 | Python logging + 本机轮转日志；`.env` / 环境变量；trace 脱敏后写 SQLite | 方便任务计划程序定位错误且不新增服务；密钥不进数据库、trace 或日志 | Langfuse/OTel：MVP 尚无远程团队观测需求，且会扩大敏感数据流转 |

运行基准是 Python 3.13；`requires-python = ">=3.11"` 表示代码包兼容范围，不表示排期要同时验证多个 Python 版本。依赖版本由 `pyproject.toml` 管理；实现期间不为每个包追新版本，只有兼容或安全问题才调整约束。

## 2. 数据流

```text
用户网页 ──保存──> profile / subscription (SQLite)
                           │ 每日运行时读取
                           ▼
             system prompt + 当日目标 + 可用工具
                           │
                           ▼
             LLM ReAct 循环 <──> 工具执行结果
                 │ 工具顺序和次数由模型决定
                 ▼
           finish(path, summary)
                 │ 校验路径、文件、内容
                 ▼
       保存 Markdown/HTML + run trace
                 │
                 ▼
              SMTP 邮件
```

程序负责身份/偏好读取、工具安全边界、步数与 token 限额、文件校验、持久化、发信和状态转换。模型负责决定何时调用哪些工具以及何时调用 `finish`。不得在 pipeline 中预先抓完固定 RSS 列表后再让模型只做总结。

## 3. 模块契约

### 3.1 存储

SQLite 使用 WAL、`busy_timeout=10s`，每个操作及时提交并关闭游标。所有写操作用参数化 SQL。

```python
class Database:
    def transaction(self): ...  # context manager，支持 BEGIN IMMEDIATE
    def fetchone(self, sql: str, params: tuple = ()) -> dict | None: ...
    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]: ...
    def execute(self, sql: str, params: tuple = ()) -> int: ...

def init_db(db: Database) -> None
def save_profile(db: Database, name: str, email: str) -> None
def get_profile(db: Database) -> dict | None
def save_subscription(db: Database, topics: list[str], keywords: list[str],
                      exclude_keywords: list[str], max_items: int) -> None
def get_subscription(db: Database) -> dict
def get_briefing(db: Database, date: str) -> dict | None
def list_briefings(db: Database, limit: int = 30) -> list[dict]
def claim_daily_run(db: Database, date: str, stale_after_minutes: int = 120) -> str
def mark_generated(db: Database, date: str, *, md_path: str, html_path: str,
                   summary: str, item_count: int) -> None
def mark_sending(db: Database, date: str) -> None
def mark_sent(db: Database, date: str) -> None
def mark_failed(db: Database, date: str, stage: str, error: str) -> None
def insert_run(db: Database, date: str, started_at: str, ended_at: str,
               status: str, trace: list[dict], error: str | None,
               *, steps: int = 0, tool_calls: int = 0,
               tokens_in: int = 0, tokens_out: int = 0) -> int
```

最小表：

- `profile(id=1, name, email, updated_at)`
- `subscription(id=1, topics_json, keywords_json, exclude_keywords_json, max_items, updated_at)`
- `briefings(date UNIQUE, status, md_path, html_path, summary, item_count, attempt,
  started_at, sent_at, failure_stage, error, updated_at)`
- `agent_runs(id, date, started_at, ended_at, status, steps, tool_calls,
  tokens_in, tokens_out, trace_json, error)`

不要另建 `articles` 去重表：MVP 只要求在当次素材中按完全相同 URL 去重；跨日是否重复可作为后续改进。用户偏好作为 JSON 数组存储，读写时验证类型和条数上限。

### 3.2 幂等与重试

`claim_daily_run` 必须在一个 `BEGIN IMMEDIATE` 事务中检查并更新当天记录，不能把“先查 sent、稍后再写”当锁。新建行时按日期写入预期的最终 Markdown/HTML 路径，因此生成前状态行也满足路径字段约束：

1. `sent` 返回 `skipped`。
2. `generating`、`generated` 或 `sending` 且 `started_at` 未超过 120 分钟，返回 `already_running`。
3. 不存在、模型阶段失败、SMTP 失败但产物缺失/不合格，或已过期的 `generating`，原子地写为 `generating` 并增加 `attempt`，返回 `generate`。
4. `generated`/`sending` 过期，或 `failure_stage=smtp` 时，若简报文件仍通过校验则原子地写为 `sending` 并增加 `attempt`，返回 `retry_send`；pipeline 复用已有文件，不再次调用 LLM。文件不合格则走 `generate` 分支。
5. `force` 不属于 MVP 接口。需要重发时先提供明确的人工操作语义，避免误发。

状态转换：`generating → generated → sending → sent`；任一失败写为 `failed` 并保存阶段与错误。重试失败运行时复用当天记录并增加 attempt，不插入违反唯一约束的新简报。claim 对既有产物的检查是轻量级的：路径位于 `data/briefings/` 内、文件存在且非空；标题、链接、条数等完整内容校验仍由 pipeline 在发送前执行。SMTP 已接受邮件但客户端未收到成功响应时可能产生重复投递；SMTP 不提供端到端 exactly-once 保证，状态记录只能减少重复，不能消除此边界。

### 3.3 Agent 与工具

```python
@dataclass
class AgentResult:
    status: str  # finished | max_steps_exceeded | token_budget_exceeded |
                 # loop_detected | incomplete | error
    steps: int
    tool_call_count: int
    tokens_in: int
    tokens_out: int
    trace: list[dict]
    briefing_path: Path | None
    summary: str | None
    error: str | None

def run_agent(client, *, system_prompt: str, user_prompt: str,
              tools_schema: list[dict], registry: ToolRegistry,
              model: str, max_steps: int, token_budget: int,
              loop_window: int) -> AgentResult
```

工具：`list_dir`、`read_file`、`search_content`、`write_file`、`bash`、`fetch_rss`、`search_web`、`fetch_url`、`finish`。`bash` 必须维持现有命令白名单、禁止 shell 拼接/管道/重定向，并限制工作目录；文件工具必须做路径包含校验。LLM 与 SMTP key 缺失时，在真正调用对应服务时返回可读错误，不影响其余模块的单测。

循环规则：

- 每轮将完整 assistant 响应追加到消息历史；DeepSeek thinking 模式需保留并回传 `reasoning_content`。不要手工重建 assistant 消息。
- assistant 返回 tool calls 时，逐个执行并把 `tool_call_id` 对应的结果加回历史；参数解析或工具错误作为工具结果交给模型处理。
- `finish` 必须是该轮唯一的 tool call；若与其它调用混在同一响应中，向模型返回格式错误并要求重新决定，避免一边结束、一边还有未处理动作。
- `finish` 是唯一成功出口。模型不再请求工具但没有调用 `finish`，返回 `incomplete`，不能当成简报成功。
- 设置 `max_steps=30`、token 预算和重复调用检测作为成本/故障保护。任一硬限制触发时停止并记录失败，不发送空白或编造的降级简报。
- trace 记录时间、工具名、脱敏后的参数、结果摘要、usage 和停止原因；不得记录 API key、SMTP 凭据或完整私密正文。原始 `reasoning_content` 不持久化到 UI/trace。
- 网络请求设连接/总超时和响应体大小上限，并拒绝 loopback、私网、link-local 等目标地址；每次跳转后重新检查地址，降低 SSRF 风险。网页搜索工具只访问配置的服务端点。

简报采用固定的轻量 Markdown 形状，便于校验和渲染：顶层标题；每条新闻用二级标题，包含来源/日期、摘要和原始 URL；当天没有符合条件的内容时，明确写“暂无符合订阅的新闻”，不能为凑数编造。`finish` 后 pipeline 验证路径位于当日工作区内、文件存在且非空、标题和每条新闻链接完整、条数不超过上限；允许合法的零条简报。验证通过后复制/原子写入 `data/briefings/YYYY-MM-DD.md` 和 HTML，再进入 `generated`。不接受模型提供的工作区外路径。用 `markdown-it-py` 关闭原始 HTML，再用 `nh3` allowlist 净化链接和标签；模型输出是外部内容，不能直接返回浏览器，也不应自己手写不完整的 HTML sanitizer。

### 3.4 提示词

系统提示只说明目标、订阅偏好、可用 RSS 源、时效/引用要求和安全约束。明确允许模型自行决定工具顺序、调用次数和是否需要网页搜索；不要写成“先抓所有 RSS，再筛选，再抓正文”的固定工作流。

每条新闻必须有原始 URL；不能确认发布日期时标注不确定或不选；按 `exclude_keywords` 排除；尽量覆盖偏好话题；同一份简报内相同 URL 只保留一次。阶段性进展可以作为后续增强，但不能要求每次都搜索历史简报。

### 3.5 Pipeline、邮件与页面

```python
@dataclass
class RunOutcome:
    status: str  # sent | skipped | already_running | dry_run | failed
    date: str
    briefing_path: Path | None
    message: str

def run_daily(date: str | None = None, *, dry_run: bool = False,
              deps: RunDeps | None = None) -> RunOutcome
```

`RunDeps` 是装配点（配置、数据库、client 工厂、发送函数、工具注册表、目录），默认为真实实现，便于测试注入假 client 与假发送函数。dry-run 使用 `<date>-dry-run` 独立工作区走真实提示词、Agent 与校验，渲染只写临时目录：不发信、不改任何正式状态（含 `agent_runs`）。

`run_daily` 是 CLI、定时任务和任何手动触发入口共用的唯一入口。未显式传入日期时按 `Asia/Shanghai` 取当天。流程为：检查必要配置和 profile/subscription → 原子 claim → 若返回 `generate` 则调 Agent、校验并保存；若返回 `retry_send` 则重校验现有文件 → 标记 `sending` → SMTP 发送 → 更新状态并保存 trace。异常路径必须 `mark_failed`，不能留下永久占用的运行状态。

邮件使用 SMTP SSL/STARTTLS 配置；身份页填写的邮箱是收件人，SMTP 用户/授权码是发件通道凭据，不混为一个字段。发送能力不暴露成 Agent 工具。重试次数和间隔配置化；SMTP 未配置时标记 `failed`，网页仍可查看已生成简报。Web 页面只做三项：身份设置、订阅设置、历史简报列表/详情。运行状态可在历史列表展示；不实现单独 trace 页面、分页 API、WebSocket 或公网鉴权体系。

## 4. 三天冲刺安排

排期日期为 **2026-10-14 至 2026-10-16**，按每天约 6–8 小时可连续投入估算。已有工具层、9 个 RSS 源和 15 项工具测试视为起点，不重复开发。目标是三天内完成可本机运行的 MVP；真实 DeepSeek、Tavily 和 SMTP 联调依赖有效凭据及网络，代码与 mock 验收不被凭据阻塞，但没有相应凭据时不能宣称真实投递验收完成。

第 1 天开工即确认 Python 环境、DeepSeek 模型可用性和 tool-calling 协议、SMTP 主机/授权码与收件地址；只验证连接或认证，不默认发送测试邮件。Tavily key 可选。这样能及早发现服务或模型契约与选型文档不一致，避免到第 3 天才发现阻塞。凭据只在本机配置，不粘贴到文档或提交仓库。

| 日期 | 当日目标与工作项 | 当日验收 / 交付 |
|---|---|---|
| **第 1 天 · 10/14** | 完成 SQLite schema/init、profile/subscription CRUD、状态机和 `claim_daily_run`；用 `BEGIN IMMEDIATE` 实现唯一运行认领、sent 跳过、SMTP-only 重试、120 分钟陈旧租约回收。补 `fetch_url` 的 DNS/IP 检查、每跳重检、超时和响应体上限。 | 存储并发测试证明同日两个 claim 只有一个可生成；租约和 SMTP 重试分支测试通过；SSRF 对 loopback/私网/link-local/跳转目标有拒绝测试；原有工具测试仍通过。 |
| **第 2 天 · 10/15** | 完成提示词与 DeepSeek client、ReAct 循环和脱敏 trace；实现 finish 输出校验、Markdown/HTML 渲染净化、SMTP 发送；接通 `run_daily` 的生成到发信路径。 | 假 client 覆盖工具结果回灌并改变下一步选择、finish、错误、循环/预算限制；非法路径/缺链接不能发送；mock SMTP 成功与失败可验证；SMTP 重试复用产物而不重跑 Agent。 |
| **第 3 天 · 10/16** | 完成 FastAPI 三页与表单、历史列表/详情、CLI dry-run/日志和退出码；补端到端 mock 流程；接入进程内 APScheduler（随 web 服务启停，单实例不重叠）；有凭据则执行一次真实服务联调。整理 README 进度和本机配置说明。 | 全量 pytest 与 Ruff 通过；TestClient 验证资料/偏好写读、历史与失败状态；dry-run 不发信不改正式状态；启动 web 后调度器注册成功、关闭即停止；逐项记录真实服务结果或未完成原因。 |

### 三天内的范围控制

- **必须交付**：本机单用户可配置、Agent 自主工具循环、简报归档/页面、SQLite 防重、CLI、测试和每日调度配置。
- **凭据到位才做真实验收**：DeepSeek 真实生成、Tavily 实际搜索、SMTP 实际投递。Tavily 是可选来源；没有它仍可验 RSS 与 Agent 主流程。
- **不纳入三天承诺**：公网部署/登录、多用户、跨日文章去重、独立 trace 控制台、第二调度器、邮件 exactly-once 保证。
- **缓冲规则**：若第 1 天原子认领或 SSRF 验收未过，第 2 天先完成安全与存储修复，压缩真实服务联调时间；若第 2 天 Agent/渲染/发送链路未通，第 3 天优先完成 mock 闭环和页面/CLI 验收，调度只做 APScheduler 接线与启动验证，不扩展功能。

## 5. 实施阶段与验收

阶段 1：补齐存储模型与迁移/初始化，测试默认值、唯一日期、状态转换、重复初始化、两个并发 claim 只有一个成功，以及超时运行可回收。

阶段 2：完善配置和提示词，测试订阅/排除词被注入、RSS 清单可用、提示没有规定工具执行顺序、缺少 key 不阻塞导入和测试。

阶段 3：完成 Agent 循环，使用假 client 覆盖 `finish`、无 finish、工具错误回灌、连续重复工具、步数上限、token 上限、reasoning 字段保留和多 tool calls。核心分支测试必须可重复，不依赖真实模型连续几天产生不同轨迹。

阶段 4：完成简报校验、Markdown/HTML 渲染、SMTP 错误处理与可 mock 的重试；验证路径穿越、空简报、缺少 URL 均不能进入发送阶段。

阶段 5：完成 pipeline/CLI，mock Agent 和 SMTP 验证成功、重复运行跳过、失败后重跑、并发启动只允许一个运行、生成失败不发信、邮件失败仍保留网页简报，且 SMTP 失败重跑只复用简报、不再次调用 Agent。

阶段 6：完成三个页面，用 FastAPI TestClient 验证表单写库、偏好回读、历史列表/详情和失败状态展示。

阶段 7：接入进程内调度：启动 `python -m news_agent.web` 时注册 APScheduler 每日任务（`DAILY_RUN_AT`，Asia/Shanghai，单实例不重叠、`MemoryJobStore`），服务关闭即停止；错过的自动触发不补跑（README 说明，手动跑 CLI）。测试覆盖时间解析、任务参数、任务函数调用 `run_daily()`、随应用启停。真实 DeepSeek、Tavily 和 SMTP 联调分别记录结果。

常用验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m news_agent.run_daily --date 2026-10-14 --dry-run
```

`--dry-run` 使用临时工作区走真实提示词、Agent 和产物校验，不发邮件也不改当天正式状态；若没有 LLM key，应在启动前给出清楚错误。包需先安装到 venv（例如 `pip install -e .`），调度器通过 `-m news_agent.run_daily` 启动，不直接执行 `src/news_agent/run_daily.py`。网络调用均有超时，总运行时限小于陈旧 claim 的 120 分钟租期，避免租约过期时旧进程仍继续发送。

## 6. 完成标准

- 页面可保存身份与订阅偏好；次次运行都从 SQLite 读取最新值。
- 每日运行能从候选 RSS/网络中由模型自主选工具，生成可查看的历史简报并发送邮件。
- 日志/trace 能还原每步工具名、结果摘要和停止原因；受控测试证明工具返回结果会影响后续模型工具选择。
- 同一日期重复触发不会并发生成或重复正常发送；失败状态可见且可重试。
- `pytest`、Ruff 检查通过；真实服务联调单独记录，不把“等待三天看轨迹变化”作为核心要求验收。

成本在完成至少 3 次真实运行后按 API usage 汇总每日报告，再估算月成本。方案阶段的 token 估算只作为预算上限，不宣称固定月费或充值可运行时长。

## 7. 外部配置

- DeepSeek API key：真实 Agent 联调。
- Tavily key：联网搜索；缺失时 RSS 和其他无 key 工具仍可运行。
- SMTP 授权码：真实邮件发送。

以上凭据通过本机环境变量配置——Windows 用户级环境变量优先（`setx` 或系统设置），项目 `.env` 可作兜底（已 gitignore；系统环境变量优先级更高）。不得写入 SQLite、trace 或日志。
