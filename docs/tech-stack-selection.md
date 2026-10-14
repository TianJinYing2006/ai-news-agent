# 每日 AI 新闻助手 Agent — 技术选型 v1.0

日期：2026-10-14
决策人：于晏
状态：选型记录；MVP 实施范围以 `docs/implementation-plan.md` 为准（2026-10-14 收敛）

> 本文保留技术考察过程，不代表其中每项建议都进入首版。MVP 只实现用户明示的身份、偏好、历史简报、每日推送和 LLM 驱动的工具循环；不为演示增加外部可观测平台、第二调度器、独立 trace 页面或跨日文章数据库。发生冲突时，以实施计划为准。

---

## 1. 决策摘要

| 层 | 选型 | 关键理由 | 否决方案 |
|---|---|---|---|
| 语言 | **Python 3.13** | 已有工具层使用 Python；RSS、正文抽取和 Web 组件可共用一个运行时 | Java 也能完成；当前没有 Java 代码或部署约束值得切换 |
| Agent 循环 | **手写 ReAct 循环** | 单循环直接显示模型调用、工具回传和停止条件，适配本项目的核心验收 | LangGraph 等框架同样能满足要求；MVP 暂无状态图、断点续跑或多 Agent 的需要，详见 §5.2 |
| LLM | **DeepSeek `deepseek-flash`**（V4.1-Flash） | OpenAI 兼容、原生 Tool Calls、缓存命中 $0.003/M | OpenAI（实测 000 不通）、Claude（403） |
| 搜索 | **Tavily** | 1000 credits/月免费、无需信用卡、返回 LLM-ready 文本 | Brave（2026-02 起需信用卡）、Serper（只给 SERP 原始数据） |
| 主新闻源 | **RSS**（免费、稳定、无 key） | 主力；Tavily 只作模型自主搜索的补充 | 纯搜索：受额度限制且覆盖不可控 |
| 存储 | **SQLite** | 零依赖、单文件，简报量级（<1 万行）绰绰有余 | PostgreSQL：为日更几十行数据起服务，过度 |
| Web | **FastAPI + Jinja2 + 少量原生 JS** | 三个表单/阅读页用服务端模板即可，无前端构建步骤 | React/Vite：当前没有复杂交互足以抵消构建和 API 状态维护 |
| 邮件 | **SMTP（脚本自带）** | 全自动、无需人工确认；脚本自包含才能被系统级调度 | ~~Agent Mail~~：需逐次人工确认，**无法自动推送**，详见 §8.1 |
| 可观测 | **SQLite 中保存精简 run trace** | 足够排障并验证模型工具选择；不增加外部服务与凭据 | MVP 暂不接 Langfuse/OTel |
| 调度 | **Windows 任务计划程序** | 单一触发入口，简单且适配本机运行 | WorkBuddy 第二触发器：增加重复运行与维护面 |

---

## 2. 环境实测依据（2026-10-14 当场跑）

**第一轮（走环境变量代理 58928）：**

```
api.openai.com        000   ← 完全不通，排除
api.deepseek.com      401   ← 通（401 = 到得了，只是缺 key），✅ 首选
api.anthropic.com     403   ← 通但被拒
api.tavily.com        200   ← 可达
http_proxy            http://127.0.0.1:58928
LLM API key 环境变量  —— 一个都没有，需要注册
```

> 注 1：代理端口是 **58928**，不是记忆中的 7897。

**第二轮（直连 vs 代理，各测 3 轮取一致结果）—— 这是实现阶段的决定性发现：**

| 目标 | 直连 | 走代理 58928 |
|---|---|---|
| `api.deepseek.com/models` | **401 ✅ 通**（401 = 到得了，缺 key） | **000 ❌** |
| `api.tavily.com` | **200 ✅ 通** | **000 ❌** |
| 国内 RSS 源（量子位/InfoQ/雷锋网等） | **200 ✅ 通** | — |
| `news.ycombinator.com`（国外源） | — | **000 ❌** |

> **注 2（重要）：代理对本项目所有目标站点都不通，且直连全部可达。**
> 所以代码里一律 `trust_env=False` 走直连。**若沿用环境变量代理，所有联网工具会集体失败。**
> 连带结论：国外 RSS 源（HN / TechCrunch 等）本机不可达，RSS 源清单全部用国内站点。
> 中文 AI 媒体会同步报道国外 AI 大新闻，覆盖度足够。

---

## 3. DeepSeek API 契约确认（含 5 个坑）

来源：`api-docs.deepseek.com/quick_start/pricing`、`/guides/tool_calls`、`/guides/thinking_mode`（2026-10-14 抓取）

### 模型与价格（USD / 1M tokens）

| 模型 | 版本 | 输入-缓存命中 | 输入-未命中 | 输出 | 上下文 | Tool Calls |
|---|---|---|---|---|---|---|
| `deepseek-flash` | DeepSeek-V4.1-Flash | $0.003 / $0.006 | $0.15 / $0.3 | $0.6 / $1.2 | 1M | ✅ |
| `deepseek-v4-pro` | DeepSeek-V4-Pro-0813 | $0.022 / $0.044 | $0.66 / $1.32 | $1.98 / $3.96 | 1M | ✅ |

- 斜杠前为**非峰值**价，斜杠后为**峰值**价，非峰值 = 峰值的一半。
- 峰值时段：UTC 01:00-04:00 与 06:00-10:00（周一至周五，中国公共假日除外）。
  → 换算北京时间 = **09:00-12:00 与 14:00-18:00**。
  → **简报任务安排在早 07:00（北京）执行，落在非峰值区，价格直接减半。**
- 并发上限：`deepseek-flash` 2500（远高于我们的单线程日更需求）。
- 旧名 `deepseek-v4-flash` 仍被接受，但底层已切换到 V4.1-Flash。**代码里统一用 `deepseek-flash`。**

### 坑 1（致命）：带 `tools` 时必须回传 `reasoning_content`

官方原文：

> If the request carries the `tools` parameter, the `reasoning_content` of all previous turns **should be passed back** to the API... **If your code does not correctly pass back `reasoning_content`, the API will return a 400 error.**

而**不带** `tools` 时行为完全相反：回传了也会被忽略。

→ 手写循环里 `messages.append(response.choices[0].message)` 一行即可正确带上 `content` + `reasoning_content` + `tool_calls` 三个字段，**不要手工重建 assistant 消息**，否则丢了 `reasoning_content` 直接 400。

### 坑 2：thinking 模式默认开启，且禁用 temperature

默认 `effort=high`。thinking 模式下 `temperature` / `presence_penalty` / `frequency_penalty` **设了不报错但无效**，`top_p` 有效区间收窄到 0.95-1.0。

→ 配置里不要写 `temperature`，写了是自我安慰。关闭 thinking 用 `extra_body={"thinking": {"type": "disabled"}}`。

### 坑 3：`content` 可能是空字符串

模型只想调工具、暂无话可说时，`content` 为 `''`（只有 `tool_calls` 有值）。日志与前端展示必须容错。

### 坑 4：`tool.function.arguments` 不保证是合法 JSON

→ 工具参数解析必须 `try: json.loads()`，失败时**把错误信息作为 tool 结果回传给模型**，让它自己重试——这正是 ReAct 循环比固定流程强的地方：模型能看见自己的格式错误并修正。

### 坑 5：`strict` 模式要换 base_url

`base_url="https://api.deepseek.com/beta"`，且 schema 里**每个 object 的所有 properties 必须进 `required`、且 `additionalProperties: false`**，否则服务端校验报错。

→ v1 先不上 `strict`（schema 写起来受限），靠坑 4 的重试机制兜底。

---

## 4. 工具集设计（9 个）

需求要求的前 5 个工具原样保留（签名自定），另加 4 个让"新闻助手"这件事真正跑得通。

| # | 工具 | 签名 | 归属 | 说明 |
|---|---|---|---|---|
| 1 | `list_dir` | `list_dir(path: str) -> str` | 必需 | 列目录。用于查看 `data/workspace/` 下已缓存的新闻、历史草稿 |
| 2 | `read_file` | `read_file(path: str) -> str` | 必需 | 读文件。读订阅偏好、读已抓取的原文 |
| 3 | `search_content` | `search_content(keyword: str, dir: str) -> str` | 必需 | 目录内全文搜索。**这是"按关键词订阅"的实现底座** |
| 4 | `write_file` | `write_file(path: str, content: str) -> str` | 必需 | 写文件。落草稿、落最终简报 |
| 5 | `bash` | `bash(command: str) -> str` | 必需 | 执行 shell。**白名单约束，见 §8 风险** |
| 6 | `fetch_rss` | `fetch_rss(feed_url: str, limit: int = 20) -> str` | 扩展 | 抓 RSS 源，返回标题/链接/摘要/发布时间 |
| 7 | `search_web` | `search_web(query: str, max_results: int = 5) -> str` | 扩展 | Tavily 搜索，返回 LLM-ready 文本。**模型"自己去搜索"的入口** |
| 8 | `fetch_url` | `fetch_url(url: str) -> str` | 扩展 | 抓网页正文（trafilatura 抽取），去广告去导航 |
| 9 | `finish` | `finish(summary: str, briefing_path: str) -> str` | 扩展 | **终止信号**。模型自己宣告"简报写完了" |

### 设计要点：为什么把"推送"排除在工具之外

`send_email` **不作为工具暴露给模型**。职责切分：

- **Agent（ReAct 循环）**：抓什么、读什么、搜什么、筛掉什么、写进哪个文件、什么时候喊停 —— **全部由模型决定**。
- **编排层（确定性 Python 代码）**：拿到 `finish` 返回的 `briefing_path` → 校验 → 渲染 HTML → 写 SQLite → 发邮件 → 更新网页。

理由：邮件是外部副作用，发送后不能由程序撤回。让 pipeline 在简报校验后发送，可以避免模型因错误 tool call 或重复调用而直接发信，也让发送结果和重试策略集中管理。需求要求每日自动推送，但服务失败时应报告失败，不以不可靠内容冒充成功。

---

## 5. ReAct 循环设计

### 5.1 为什么必须是闭环，而不是一次性规划

需求的字面要求是「工具调用顺序由 LLM 决定」。**满足它的方式有两种，ReAct 只是其中一种**——
所以这里必须把选择闭环的理由讲清楚，否则就是跟风。

| | 开环（一次性规划） | 闭环（ReAct） |
|---|---|---|
| 模型决策次数 | 1 次 | N 次 |
| 决策时能否看到执行结果 | **不能** | 能 |
| 顺序由谁决定 | 模型 | 模型 |
| 控制论类比 | 开环控制（盲控） | 闭环控制（反馈控制） |
| 失败恢复 | 靠外围代码兜底 | **模型自己看见错误并换策略** |

**关键认知：差别不在"谁决定顺序"——两者都是模型决定。差别在"模型能不能看见自己行为的后果"。**

#### 三个场景说明为什么这个任务需要闭环

| 场景 | 开环会怎样 | 闭环会怎样 |
|---|---|---|
| 某个 RSS 源 404 或改版 | 计划已定死，要么整轮失败，要么靠外围代码写兜底逻辑——**兜底一写，就变回硬编码了** | 模型看到"源返回 404"，自己决定换源或改用 `search_web` |
| 今天到底有多少条值得看 | 抓多少条是**在没看到内容时盲定的**——定 5 条，今天可能只有 2 条有价值的，也可能爆发了 10 条 | 抓一轮看内容，自己判断"够了"或"Agent 方向太少，再搜个别的关键��" |
| 正文是付费墙 / JS 渲染 | `fetch_url` 只拿到 50 字，计划里没有应对分支 | 模型看到"正文抽取失败"，自己改用 RSS 摘要或换一条新闻 |

这三个场景正好对应 §11 的验收标准：trace 里**因内容而异的分支**，只有闭环才可能出现。

#### 诚实边界：什么时候开环就够

不装。如果任务同时满足下面三条，开环确实够用，而且**更稳定、更便宜、更可预测**：

1. 工具调用之间没有依赖（A 的结果不影响 B 的参数）
2. 工具不会失败，或者失败时你不需要模型来恢复
3. 你事先知道确切需要几步

反例：「每天早上抓这 5 个固定 RSS 源，然后总结」——这开环就能跑，甚至写固定脚本都行，上 Agent 是杀鸡用牛刀。

#### 本项目的判断

用户明确要求模型决定工具调用顺序，因此 MVP 采用闭环：模型发起工具调用，看到结果后再决定下一步。RSS、搜索和正文工具同时提供给模型，但程序不预先要求“先并行抓取固定数量的源”。模型可以在一轮请求中自行发起多个彼此独立的调用，也可以根据结果逐步搜索或结束。

纯脚本式“固定抓取 RSS → 汇总”实现更简单、成本更可预测，但不满足本项目的核心验收要求；在工具可选和顺序自由得到测试的前提下，手写循环已足够，不需要再叠加工作流框架。

#### 附：ReAct 的 "Reason" 那一半在我们这怎么落地

ReAct = **Reason** + Act。我们的实现里：

- **Act** = `tool_calls`（那 9 个工具）
- **Reason** = DeepSeek thinking 模式产出的 `reasoning_content`

注意我们没有让模型在 `content` 里显式输出 "Thought: ..." 文本，而是**依赖 thinking 模式的内在推理链**。
这与 ReAct 原论文"显式输出 Thought"的写法不同，但与 DeepSeek 的 API 设计契合——
`reasoning_content` 是模型内部 CoT，不占用 `content` 通道，也不需要额外的提示词约束。

代价：`reasoning_content` 必须严格回传（见 §3 坑 1），且不能设 `temperature`。

### 5.2 为什么 MVP 手写循环，何时改用框架

手写的理由是范围小：一个 Agent、一份消息历史、一组 function tools、一个循环和明确的成本上限。代码直接展示“请求模型 → 执行它选的工具 → 回传结果 → 再请求模型”，便于核对核心要求，也便于适配 DeepSeek 的 `reasoning_content` 消息格式。

框架并不天然违反“工具顺序由 LLM 决定”。LangGraph 条件边可以根据模型响应中的 `tool_calls` 路由到工具节点；只要 Python 没有预设具体要调用哪些工具或调用顺序，仍是模型在选择动作。不能仅凭代码中出现条件判断就判定为硬编码流程。

| 方案 | 适用点 | 本项目暂不选的原因 |
|---|---|---|
| 手写 ReAct + OpenAI 兼容 SDK | 单循环、控制流和消息格式直接可见 | 需要自行负责消息协议、重试和 trace；本项目范围小且已有工具注册表，成本可控 |
| LangGraph | 多节点状态图、断点续跑、人工介入或复杂分支 | 当前没有这些需求；会增加状态模型和运行时概念，但并非不符合核心要求 |
| LangChain AgentExecutor | 常见 Agent 抽象和工具适配 | 当前只需要一个循环，框架带来的执行抽象没有相应的复杂度可抵消 |
| Pydantic AI | 类型化工具参数和 Agent API | 是合理替代，但当前工具已有 JSON Schema 注册表；迁移会替换循环和测试而不增加必要能力 |
| CrewAI / AutoGen | 多 Agent 协作 | 当前是单 Agent；角色分工尚未带来可验证收益 |

如果以后需要中断恢复、并行多 Agent、人工审批或多人长期维护，再比较框架的状态持久化、可观测与迁移成本。MVP 不是因为框架做不到，而是当前单循环不需要它们的主要能力。

### 5.3 核心循环（`src/news_agent/agent/loop.py`）

```python
def run_agent(system_prompt, user_prompt, tools, max_steps=30, token_budget=800_000):
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt}]
    trace = []

    for step in range(1, max_steps + 1):
        resp = client.chat.completions.create(
            model=cfg.model, messages=messages, tools=TOOL_SCHEMAS,
            extra_body={"thinking": {"type": "enabled"}},
        )
        msg = resp.choices[0].message
        messages.append(msg)          # ⚠️ 整条 append，保住 reasoning_content（坑 1）
        trace.append(step_record(step, msg, resp.usage))

        # 停止条件 1：模型不再要工具但没调 finish → 不算成功
        if not msg.tool_calls:
            return AgentResult(status="incomplete", trace=trace)

        names = [c.function.name for c in msg.tool_calls]

        # finish 必须是该轮唯一的调用：避免"一边结束、一边还有未处理动作"
        if "finish" in names and len(msg.tool_calls) > 1:
            for call in msg.tool_calls:
                # 每个 tool_call_id 都必须有对应结果，否则下一轮请求 400
                content = (
                    "[已忽略] 本轮混入了其它工具调用，finish 未生效。请重新决定。"
                    if call.function.name == "finish"
                    else "[未执行] finish 必须是本轮唯一的工具调用。请重新决定："
                         "先完成剩余动作，或只调用 finish。"
                )
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            continue

        for call in msg.tool_calls:
            result = dispatch(call)   # 内部已含坑 4 的 JSON 容错 + 白名单校验
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })
            if call.function.name == "finish":
                return AgentResult(status="finished", trace=trace,
                                   briefing_path=parse_finish(result))

        # 停止条件 2：token 预算耗尽
        if total_tokens(trace) > token_budget:
            return AgentResult(status="token_budget_exceeded", trace=trace)

        # 停止条件 3：死循环检测（连续 3 步工具调用指纹相同）
        if detect_loop(trace):
            return AgentResult(status="loop_detected", trace=trace)

    return AgentResult(status="max_steps_exceeded", trace=trace)
```

### 三个硬闸（与 DeepResearch 的硬闸设计一脉相承）

| 闸 | 阈值 | 触发后行为 |
|---|---|---|
| `max_steps` | 30 步 | 强制收尾，标记 `max_steps_exceeded` |
| `token_budget` | 800K tokens | 强制收尾，标记 `token_budget_exceeded` |
| `detect_loop` | 连续 3 步相同工具+相同参数指纹 | 强制收尾，标记 `loop_detected` |

任何一个闸触发，循环会停止并记录失败状态与 trace。只有简报通过内容和路径校验后才发信；失败时不发送空白或编造的降级内容，用户仍可在网页看到错误状态和已生成文件（若有）。

### 提示词骨架（`agent/prompt.py`）

系统提示只给**目标与约束**，不给步骤：

```
你是每日 AI 新闻简报助手。目标：为 {user_name} 产出一份今天值得看的 AI 新闻简报。

订阅偏好：
- 关注话题：{topics}
- 关键词：{keywords}
- 排除关键词：{exclude_keywords}
- 目标条数：{max_items}

可用 RSS 源（可按需选择）：
{feeds_catalog}

工作区：data/workspace/{date}/
约束：
1. 只选今天或昨天发布的内容，不要历史新闻充数。
2. 每条必须给出可点击的原始链接，禁止编造 URL。
3. 按订阅偏好选择并整理新闻；可用 RSS、搜索或正文工具补足信息。
4. 简报写入 data/workspace/{date}/briefing.md，然后调用 finish 宣告完成。

你不必须按任何固定顺序工作。你自己决定使用哪些工具、何时使用和何时信息已经足够。
```

提示词提供目标、偏好和候选源，不预设工具调用序列。具体顺序和次数由模型根据工具结果决定。

### 5.4 跨运行记忆：为什么不用 Checkpointer

先澄清一个容易混淆的点 —— **「用户订阅偏好要存下来」不等于「需要 Checkpointer」**：

| | 订阅偏好持久化 | Checkpointer 会话持久化 |
|---|---|---|
| 存的是什么 | 业务数据（话题、关键词） | **执行状态**（messages 列表、graph state） |
| 目的 | 用户配置跨次运行生效 | 任务中断后**从断点接着跑** |
| 谁来做 | 一张 SQL 表，任何 Web 应用的基本功 | 框架提供的运行时能力 |
| 跟 Agent 框架有关吗 | **无关** | 有关 |

订阅偏好是**业务 CRUD**，框架不会替代这部分应用数据管理；本机单用户只需一份 `subscription` 配置。

#### 我们真正的跨运行需求，以及解法

MVP 的跨运行记忆只要求保存用户偏好和历史简报。Agent 可以通过文件工具查看历史，但不要求每次运行都扫描历史或维护已推送文章索引：

| # | 需求 | 落点 |
|---|---|---|
| 1 | 用户偏好跨日生效 | SQLite `subscription` 表 |
| 2 | 用户查看历史；Agent 若需前情可按需了解 | `data/briefings/` 归档文件 |

> 注：第 2 条在初次梳理时被归进「前端功能」而漏算。它其实是**双重身份**——
> 既是给用户看的历史记录，也是 Agent 的跨运行记忆来源。见下方专门说明。

解法不是存会话，而是**把跨运行记忆从「会话状态层」下沉到「业务数据层」**：

- 不存 messages；保存用户配置、简报状态和必要的运行 trace
- 每次运行仍是**全新会话**，Agent 通过 `search_content` 工具按需查历史
- 好处：会话恒定干净、可重放、可调试；不会因累积 30 天 messages 而爆 token

如果硬要用 Checkpointer 存整个会话，代价是：

| 问题 | 后果 |
|---|---|
| messages 逐次累积 | token 成本随时间**线性上升**，日更场景尤其致命 |
| 状态漂移 | 某次异常的 messages 会被永久带进后续所有运行 |
| 重放困难 | 想复现第 30 天的行为，得先备齐前 29 天的 state |

**这个下沉是架构判断，不是偷懒。** 判断标准只有一条：

> 需要跨运行保留的是**事实**（推过什么、用户喜欢什么），还是**推理过程**（模型的思考轨迹）？

本项目不需要跨日恢复模型会话 → 用业务表保存偏好和简报。**只有要从中断点恢复一轮长任务时，才需要 Checkpointer。**

> 补一句：推理轨迹我们也不是不要——它落在 `agent_runs.trace_json` 里用于排障与验收，
> 但**不会被喂回下一轮对话**。这是「可观测」与「可恢复」的关键区别。

#### 历史简报的双重身份

一份简报产出后有 **两个消费方**，需求完全不同：

| 消费方 | 通路 | 要求 |
|---|---|---|
| 用户 | `briefings` 表 → 网页列表 | 按日期浏览、能读全文 |
| **明天的 Agent** | `data/briefings/` 文件 → `search_content` 工具 | **按需检索片段**，不是全量读取 |

若希望 Agent 能按需了解前情，简报文件比只存数据库更直接：`list_dir` / `search_content` 操作磁盘文件，不走 SQL。历史页本身也需要可读取的正文，因此 Markdown/HTML 文件同时服务网页和 Agent 工具，不额外建设数据库全文检索。

这个约束反过来强化了 §4 的工具集设计：需求里给的 `list_dir` / `read_file` / `search_content`
看着像通用文件工具，其实是**为 Agent 提供跨运行记忆的通道**。

#### 存储策略：文件为主，数据库存指针

```
data/briefings/2026-10-14.md     ← Agent 可 grep 的正文
data/briefings/2026-10-14.html   ← 邮件与网页用的渲染结果
briefings 表                      ← 存 date / status / path / summary / item_count，不存正文
```

| 角度 | 理由 |
|---|---|
| Agent 侧 | `search_content("Agent", "data/briefings")` 直接 grep，无需 SQL 中转 |
| 用户侧 | 网页查表拿元数据与路径再读文件，DB 保持轻量（一年约 365 行） |
| 可备份 | 简报是纯文本，压缩目录即可，不用导出数据库 |
| 容量 | md ~5KB + html ~15KB/天，一年约 7MB，毫无压力 |

#### 累积风险与对策

| 风险 | 对策 |
|---|---|
| 归档越来越多，grep 变慢、噪声变大 | `search_content` 限定文件类型和返回片段；若实际变慢，再按日期分目录或加索引 |
| Agent 全量读入历史简报 → token 爆炸 | `search_content` 只返回**命中片段**并截断，不返回整篇（工具层保证） |
| 文件与数据库不一致 | 编排层保证「先写文件成功，再写数据库」，数据库退化为索引 |

历史页面读取归档文件；trace 用于排障。循环是否把工具结果交回模型并把下一次选择交给模型，由受控的循环测试验证，不能只凭自然运行轨迹证明。

### 5.5 订阅偏好怎么注入 Agent

偏好摘要直接进入系统提示：

| 通道 | 做法 | 理由 |
|---|---|---|
| **系统提示摘要** | 编排层读 `subscription` → 渲染进 system prompt | 偏好是筛选硬约束，模型每次请求都应直接收到 |

当前单用户订阅数据很小，直接放入提示词更可靠，避免增加 `preferences.md` 的生成、读取和同步逻辑。若后续允许大量关键词，再根据 token 用量考虑文件摘要或分层检索。

### 5.6 阶段性新闻与跨日去重（后续增强）

MVP 在一份简报内部按完全相同 URL 去重，不做跨日文章索引，也不要求每次运行都检索历史。这样避免在尚未验证用户是否遇到重复问题前引入 `articles` 表、额外写入路径和永久保留策略。

历史简报仍以文件保存，Agent 可以通过 `search_content` 按需查前情。若实际使用中发现跨日重复或事件上下文断裂，再评估最近标题索引或 URL 去重表；相关进展应正常推送，只在确认关联时加前情说明。标题相似度去重不作为方案，因为它可能误删同一事件的新进展。

---

## 6. 目录结构

```
news_agent/
├── docs/
│   └── tech-stack-selection.md      ← 本文档
├── src/news_agent/
│   ├── config.py                    # 环境变量 + 默认配置
│   ├── agent/
│   │   ├── loop.py                  # ⭐ ReAct 循环（核心）
│   │   └── prompt.py                # 系统提示模板
│   ├── tools/
│   │   ├── registry.py              # 注册表 + JSON Schema 生成
│   │   ├── filesystem.py            # list_dir / read_file / search_content / write_file
│   │   ├── shell.py                 # bash（白名单）
│   │   ├── news.py                  # fetch_rss / search_web / fetch_url
│   │   └── control.py               # finish
│   ├── sources/
│   │   └── feeds.py                 # RSS 源清单（实测可用 9 个，已剔除 7 个）
│   ├── storage/
│   │   ├── db.py                    # SQLite 连接与迁移
│   │   └── models.py                # 表定义
│   ├── delivery/
│   │   ├── render.py                # Markdown → HTML
│   │   └── email.py                 # SMTP 发送
│   ├── web/
│   │   ├── app.py                   # FastAPI：身份 / 订阅设置 / 历史简报
│   │   └── static/
│   ├── pipeline.py                  # 编排层：run agent → 渲染 → 落库 → 发信
│   └── run_daily.py                 # CLI 入口
├── tests/
│   ├── test_loop.py                 # 循环停止条件单测（mock LLM）
│   └── test_tools.py                # 工具单测（含 bash 白名单）
├── data/
│   ├── news_agent.db                # SQLite
│   └── workspace/{date}/            # Agent 工作区
├── pyproject.toml
└── .env.example
```

---

## 7. 数据模型（SQLite）

```sql
-- 本机唯一用户身份；id 固定为 1，不开放账户注册
CREATE TABLE profile (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  name        TEXT NOT NULL,
  email       TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- 订阅偏好
CREATE TABLE subscription (
  id               INTEGER PRIMARY KEY CHECK (id = 1),
  topics           TEXT NOT NULL,   -- JSON array
  keywords         TEXT NOT NULL,   -- JSON array
  exclude_keywords TEXT NOT NULL,   -- JSON array
  max_items        INTEGER NOT NULL DEFAULT 8,
  updated_at       TEXT NOT NULL
);

-- 简报（正文存文件、表只存指针与元数据，理由见 §5.4「历史简报的双重身份」）
CREATE TABLE briefings (
  id           INTEGER PRIMARY KEY,
  date         TEXT NOT NULL,        -- YYYY-MM-DD
  status       TEXT NOT NULL CHECK (status IN ('generating','generated','sending','sent','failed')),
  md_path      TEXT NOT NULL,        -- data/briefings/2026-10-14.md，Agent grep 用
  html_path    TEXT NOT NULL,        -- data/briefings/2026-10-14.html，邮件/网页用
  summary      TEXT,                 -- 首条摘要，列表页展示用，避免读全文
  item_count   INTEGER,
  attempt      INTEGER NOT NULL DEFAULT 1,
  started_at   TEXT NOT NULL,
  sent_at      TEXT,
  failure_stage TEXT,
  error        TEXT,
  updated_at   TEXT NOT NULL,
  UNIQUE(date)
);

-- 精简 Agent 运行轨迹：本地排障与行为验证
CREATE TABLE agent_runs (
  id             INTEGER PRIMARY KEY,
  date           TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  ended_at       TEXT,
  status         TEXT NOT NULL,      -- finished|incomplete|max_steps_exceeded|
                                     -- token_budget_exceeded|loop_detected|error
  steps          INTEGER,
  tool_calls     INTEGER,
  tokens_in      INTEGER,
  tokens_out     INTEGER,
  trace_json     TEXT,               -- 脱敏后的工具调用摘要
  error          TEXT
);
```

`agent_runs.trace_json` 只保存工具名、脱敏参数、结果摘要和 token usage，不保存 API 凭据、私密正文或原始 `reasoning_content`。MVP 在同一份简报中按完全相同 URL 去重；跨日文章索引留待实际出现重复推送问题后再评估。

---

## 7.1 前端规格（需求明文要求）

> 需求原文：「配一个简单的前端页面即可——用户能填身份、设置订阅偏好、查看历史简报。
> 不需要做得多好看。」

技术口径：**Jinja2 服务端渲染页面 + `fetch` 调 API 做交互**。
不引入构建工具、不写前端路由——符合"不需要做得多好看"的定调。

### 三个页面（需求范围）

| 页面 | 路由 | 字段 / 内容 | 落库或读取 |
|---|---|---|---|
| **① 身份** | `/settings/profile` | 称呼 `name`、邮箱 `email` | 写 `profile` |
| **② 订阅偏好** | `/settings/subscription` | 关注话题 `topics[]`（预设标签多选）<br>关键词 `keywords[]`（自由输入）<br>排除关键词 `exclude_keywords[]`<br>每日条数 `max_items` | 写 `subscription` |
| **③ 历史简报** | `/` 与 `/briefings/{date}` | 日期列表 → 点开看渲染后的 HTML | 读 `briefings` 表 + `html_path` 文件 |

### API

```
GET  /                          历史简报列表页
GET  /briefings/{date}          单篇简报页
GET  /settings/profile          身份设置页
GET  /settings/subscription     订阅设置页
POST /api/profile               保存身份
POST /api/subscription          保存订阅偏好
GET  /api/briefings             简报列表（date / summary / item_count / status）
GET  /api/briefings/{date}      单篇 HTML 正文
```

日常运行入口为 CLI/任务计划程序的 `pipeline.run_daily()`。MVP 不增加网页手动触发接口；开发时直接运行同一个 CLI。

### 实现注意点

| 注意点 | 处理 |
|---|---|
| **SQLite 并发写** | 用户在页面改订阅时，Agent 可能正在跑。开 WAL 模式 + `busy_timeout=10s`，写任务用事务保护 |
| **监听地址** | 默认 `127.0.0.1:8000`，**不对外暴露**，因此零鉴权。若要局域网/公网访问，必须先加一层 token |
| **简报正文不入库** | 按 §5.4，正文在 `data/briefings/*.html`；API 按 `html_path` 读文件返回 |
| **运行状态** | 历史简报列表展示生成中、已发送或失败状态；不引入 WebSocket |

### 订阅偏好改动何时生效

不需要任何同步机制：Agent 每次运行开始时读一次 `subscription` 表。
用户晚上改偏好，第二天早上 7 点自然生效。

---

## 8. 风险与对策

| # | 风险 | 等级 | 对策 |
|---|---|---|---|
| 1 | `reasoning_content` 未回传 → 400 | **高** | 整条 `messages.append(msg)`，禁止手工重建 assistant 消息；单测断言消息里含该字段 |
| 2 | `bash` 工具被模型用来搞破坏 | **高** | 真实 Bash 子进程执行命令白名单（`ls/cat/grep/wc/date/head/tail`）；路径解析后锁在 `data/`；禁止管道、重定向、命令替换与拼接；清理子进程环境、限制超时和输出；不开放写入或网络命令 |
| 3 | 模型死循环狂烧钱 | 中 | 三道硬闸（max_steps / token_budget / loop 指纹检测） |
| 4 | RSS 源失效或改版 | 中 | 9 个实测可用源（另有 7 个已剔除并记录原因）；`fetch_rss` 失败返回明确错误，让模型自己换源或用 `search_web` 兜底 |
| 5 | 抓到付费墙/JS 渲染页面 | 低 | `fetch_url` 抽取正文长度 < 200 字符时返回"正文抓取失败"提示，模型自行改用 RSS 摘要 |
| 6 | 邮件发送失败 | 中 | 重试 2 次；仍失败则把简报留在网页并在 `agent_runs` 记 error，下不静默 |
| 7 | Tavily 免费额度耗尽 | 低 | 搜索工具返回明确错误；Agent 可继续使用 RSS，联网搜索不是生成简报的硬依赖 |
| 8 | 代理端口变动（历史上有过 7897→58928） | 低 | 代理地址走环境变量，不硬编码 |
| 9 | 手动运行与定时任务并发 | 中 | 唯一调度入口 + SQLite 原子 claim；`sent` 跳过，活动中的运行返回 `already_running` |

---

## 8.1 每日自动推送：通道、调度与幂等

### 通道选型修正：Agent Mail 不能用（2026-10-14 实测）

原定用「已连接」的 Agent Mail，实测发现两个阻断问题：

| 问题 | 实测结果 | 影响 |
|---|---|---|
| 邮箱未开通 | `GetMe` 返回 `{"status":"not_bound"}`：「Agent 邮箱尚未开通」 | 连接器已连，但邮箱服务未开通 |
| **发送需人工确认** | `SendMessage` 文档：收到 `CONFIRMATION_REQUIRED` 后必须展示摘要、**显式询问用户**、拿到 `confirmation_token` 才能重发 | **每封邮件都要人在场 → 与"自动推送"直接冲突** |

第二条是致命的：自动化在早上 7 点跑，没人点确认。

> 教训：连接器状态显示 `connected` ≠ 该服务能满足需求。选型时要验证**调用契约**，
> 不能只看连接状态。

→ **改用脚本自带 SMTP。**

### SMTP 通道配置

| 邮箱 | 服务器 | 端口 | 凭据获取 |
|---|---|---|---|
| **QQ 邮箱（推荐）** | smtp.qq.com | 465 (SSL) | 设置 → 账户 → 开启 IMAP/SMTP → 生成**授权码**（不是登录密码） |
| 163 邮箱 | smtp.163.com | 465 (SSL) | 设置 → POP3/SMTP → 开启 → 授权码 |
| Gmail | smtp.gmail.com | 587 (STARTTLS) | 需应用专用密码 |

推荐 QQ 邮箱：国内可达、配置 5 分钟、每日一封不触发风控。

`.env` 新增项：

```bash
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=sender@qq.com
SMTP_PASS=xxxxxxxx        # 授权码，不是邮箱密码
MAIL_FROM_NAME=每日 AI 简报
```

收件地址使用网页身份设置中的 `profile.email`，不再在环境变量和网页中各维护一份，避免配置不一致。

### 调度、原子认领与邮件状态

MVP 只使用 Windows 任务计划程序。它随系统启动、可配置唤醒机器，且能直接运行本机 venv；WorkBuddy 第二触发器不会提高生成质量，只会增加启动入口和重复运行的可能。

重复执行通过 SQLite 原子认领解决。仅做 `SELECT status` 再运行有竞态：两个进程可能同时读到未发送。`claim_daily_run()` 在 `BEGIN IMMEDIATE` 事务中检查并写入状态：已 `sent` 返回 `skipped`；活动中的 `generating/generated/sending` 返回 `already_running`；模型阶段失败后重新生成，SMTP 失败且文件仍有效则只重试发送，不再付一次模型调用成本。超过 120 分钟的陈旧运行可回收，防止进程崩溃后永久卡住；总运行时限必须短于该租期。

```text
generating → generated → sending → sent
      └────────任一阶段失败────────→ failed → 重试时重新认领
```

流程顺序：先把校验后的 Markdown/HTML 原子写入文件并记录 `generated`，再发 SMTP 邮件，收到成功响应后写 `sent`。邮件发送失败时保留简报并标记 `failed`，不丢失用户可读内容。若 SMTP 服务已接收邮件但连接在确认前中断，客户端无法判断邮件是否已投递；可能出现重复投递，因此不宣称 exactly-once。

Agent 不直接发邮件。这样可以让模型只负责收集和整理，程序统一校验简报、控制重试并记录外部副作用。遇到模型/搜索/SMTP 错误时不发送空白或编造内容。

Windows 任务应设置 venv Python、模块入口及工作目录。项目安装到 venv 后使用 `-m`，避免直接执行包内脚本导致相对导入失败：

```powershell
schtasks /Create /TN "NewsAgentDaily" /TR '"D:\项目\news_agent\.venv\Scripts\python.exe" -m news_agent.run_daily' /SC DAILY /ST 07:00 /F
```

任务属性中将“起始于”设置为 `D:\项目\news_agent`，并先通过任务计划程序手动运行验证日志和退出码。

---

## 9. 成本账

旧估算按每日 1 次、约 30 次 API 调用、平均 20K 输入 / 500 输出计算。它只用于展示数量级，不是稳定成本承诺：实际输入会受对话历史长度、缓存命中、模型版本和调用次数影响。

| 项 | 单价（非峰值） | 日耗量 | 日成本 |
|---|---|---|---|
| 输入（缓存未命中） | $0.15/M | ~600K | $0.090 |
| 输出 | $0.6/M | ~15K | $0.009 |
| RSS | 免费 | — | $0 |
| Tavily | 免费额度内 | ~10 次 | $0 |
| **合计** | | | **≈ $0.10/天 ≈ ¥0.72/天 ≈ ¥21/月** |

按表中粗略值约 ¥0.72/天，即约 ¥21/月；¥50 对应约两个月多，而不是半年。缓存可能降低成本，但必须根据实际 usage 统计，不能在没有运行数据时预估具体节省比例。MVP 在至少三次真实运行后按输入/输出 token 和搜索调用更新成本估算。

---

## 10. 落地顺序与验收依据

具体文件、接口、阶段和命令以 `docs/implementation-plan.md` 为准。凭据只在真实联调阶段需要：DeepSeek API key、可选 Tavily key、SMTP 授权码。代码和单测应先在缺少这些凭据的环境中完成。

核心要求是模型在收到工具结果后仍能选择下一步。用假 client 构造两条可重复路径：一个工具结果导致模型选择搜索，另一个结果导致模型直接结束；再检查循环把结果回传、把后续 tool call 交给模型，而没有在 Python 里规定调用顺序。真实 trace 用于排障，不以连续几天工具序列必须不同作为证明。
