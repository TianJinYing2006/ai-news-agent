# ai-news-agent

每日 AI 新闻简报 Agent：每天自动整理一份 AI 新闻简报并推送到邮箱。

核心设计点是**工具调用顺序完全由大模型决定**——不是"先抓完 RSS，再筛选，再总结"的固定流水线，
而是手写 ReAct 循环：模型发起工具调用、看到真实结果、再决定下一步（继续搜、换源、抓正文、还是结束）。

> 状态：进行中。已完成技术选型、工具层与源清单；存储层、Agent 循环、编排层、前端与调度开发中。
> 详见下方「当前进度」。

---

## 为什么手写循环而不是用框架

不是因为框架做不到——LangGraph 的条件边同样可以依据模型响应中的 `tool_calls` 路由，
只要不预设"调哪个工具"，就仍是模型在选择动作。

选手写是因为**范围足够小**：单 Agent、单份消息历史、一组 function tools、一个循环和明确的成本上限。
这样"请求模型 → 执行它选的工具 → 回传结果 → 再请求模型"每一步都直接可见，
也便于适配 DeepSeek 的消息协议细节。若日后需要断点续跑、人工审批或多 Agent 协作，再评估框架。

## 工具集（9 个）

需求指定的 5 个原样保留，另加 4 个让"新闻助手"真正成立。

| 工具 | 作用 |
|---|---|
| `list_dir` / `read_file` / `write_file` | 工作区文件读写 |
| `search_content` | 目录内全文检索，返回**命中片段**而非整篇 |
| `bash` | 受限命令（`ls/cat/grep/wc/date/head/tail`），不启动任意 shell |
| `fetch_rss` | 抓取 RSS/Atom 源 |
| `search_web` | Tavily 联网搜索，供模型按需补搜 |
| `fetch_url` | 抽取网页正文（trafilatura） |
| `finish` | 宣告简报完成，是循环的唯一成功出口 |

**`send_email` 不在工具列表里。** 邮件是不可撤销的外部副作用，交给编排层在简报校验通过后执行；
模型只负责把简报写到文件然后喊停。

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.13 |
| Agent 循环 | 手写 ReAct + OpenAI 兼容 SDK |
| LLM | DeepSeek `deepseek-flash`（原生 Tool Calls） |
| 搜索 | Tavily（1000 credits/月免费） |
| 主新闻源 | RSS（9 个实测可用的国内源） |
| 存储 | SQLite（WAL，原子认领 + 陈旧租约回收） |
| Web | FastAPI + Jinja2，只监听 `127.0.0.1` |
| 推送 | SMTP（脚本自带） |
| 调度 | Windows 任务计划程序 |

## 数据流

```
用户网页 ──保存──> profile / subscription (SQLite)
                          │ 每日运行时读取
                          ▼
            system prompt + 可用工具（不含执行顺序）
                          │
                          ▼
            LLM ReAct 循环 <──> 工具执行结果
                │ 工具顺序和次数由模型决定
                ▼
          finish(path, summary)
                │ 校验路径、文件、内容
                ▼
      保存 Markdown/HTML + 精简 run trace
                │
                ▼
             SMTP 邮件
```

## 两个值得说明的设计取舍

**闭环而非一次性规划。** 两者的工具顺序都由模型决定，差别在于模型能否看见自己行为的后果。
RSS 源 404、正文是付费墙、今天内容特别少——这些情况下只有闭环能让模型自己换策略，
而不必由外围代码写兜底（兜底一写就变回硬编码）。

**跨运行记忆下沉到业务数据层。** 每次运行都是全新会话，不保存 `messages`；
偏好和简报状态存 SQLite，简报正文存文件供 `search_content` 检索。
这样会话恒定干净、可重放，也不会因为累积历史消息而让 token 成本线性上升。

## 如何验证"顺序真的由模型决定"

不靠"连跑几天看轨迹是否不同"——那依赖不可控的真实模型行为。

改用**受控测试**：用假 client 构造两条路径，一个工具结果导致模型选择搜索，
另一个结果导致模型直接结束；断言循环确实把结果回传、并把下一次工具选择交给模型，
而没有在 Python 里规定调用顺序。真实 trace 只用于排障。

## 快速开始

```bash
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e .
cp .env.example .env      # 填入 DEEPSEEK_API_KEY / TAVILY_API_KEY / SMTP 授权码

./.venv/Scripts/python.exe -m pytest -q          # 单测
./.venv/Scripts/python.exe -m news_agent.run_daily --dry-run
```

需要三个凭据，但**都不阻塞代码编写与单测**：DeepSeek API key、Tavily key（可选）、
QQ 邮箱 SMTP 授权码。

## 当前进度

| 模块 | 状态 |
|---|---|
| 技术选型与实施计划 | ✅ `docs/` |
| 工具层 9 个 + 15 项单测 | ✅ |
| RSS 源清单（9 个可用 / 7 个已剔除并记录原因） | ✅ |
| 存储层（原子认领、状态机、租约回收） | ⏳ |
| Agent 循环（停止条件、错误回灌、trace） | ⏳ |
| 简报校验、渲染（Markdown→HTML 净化）、SMTP | ⏳ |
| 编排层与 CLI | ⏳ |
| 前端三页（身份 / 订阅 / 历史简报） | ⏳ |
| 调度与真实服务联调 | ⏳ |

## 文档

- `docs/tech-stack-selection.md` —— 选型依据与关键设计论证
- `docs/implementation-plan.md` —— 实施阶段、接口契约与验收标准（**以这份为准**）
