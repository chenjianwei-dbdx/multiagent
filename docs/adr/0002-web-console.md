# ADR 0002: 本地 Web 控制台（Loopback Console）

日期：2026-09-30
状态：已接受（用户决策 2026-09-30；推翻 ADR 0001「MVP 只做 CLI，Web 后置」中的后置条款）
关联：ADR 0001（D19、D25）、[v1.1 收敛方案](../01-技术收敛与详细开发方案.md)、[dependency-baseline.md](../dependency-baseline.md)

## 背景

P0–P5 完成后，系统交付形态是 `omas` CLI：提交任务、运行、应答缺槽追问、导出交付物全部
经终端命令完成。缺槽应答在 CLI 里尤其沉重——用户必须手工拼 `decision_id`、
`expected_epoch`、`awaiting_event_id` 三个参数（`omas task respond`），少一个即报错。

用户需要浏览器形态的交互：一个意图输入框 + 文件上传 + 等待结果 + 回答追问 + 下载交付物。

ADR 0001 曾把 Web 后置为"非 MVP"。本 ADR 将其提前，但**只提前到"本地单用户控制台"
的程度，不提前到多用户在线服务**。

## 决策

1. **新增 `omas web` 子命令**，启动一个 Starlette 应用（`src/omas/web/`），默认绑定
   `127.0.0.1:8000`，仅允许 loopback 地址（`127.0.0.1` / `localhost` / `::1`）；
   拒绝绑定到任何非回环地址——这不是多用户服务，不引入鉴权、会话、多租户。

2. **服务器渲染 HTML，无 JS 工具链**：Jinja2 模板 + 少量 vanilla JS（事件轮询、自动
   刷新）。不引入 Node/npm/构建步骤；`src/omas/web/static/` 是纯静态资源。

3. **零新增外部依赖**：starlette / uvicorn / python-multipart / jinja2 / httpx 复用
   `uv.lock` 中已有的包（原为 pydantic-ai→mcp 的传递依赖）。本次只把它们**提升为
   omas 的直接依赖**，不引入 fastapi、不新增任何未锁定包（实测 PyPI 访问极慢，
   全新依赖不可行；见 dependency-baseline.md）。

4. **页面仅两屏**（MVP 范围）：
   - 提交页：模板下拉（枚举 `template_versions`）、意图输入框、材料文件上传（多选）、
     数据策略复选框；
   - 任务页：状态/epoch/数据策略/材料数、事件流、缺槽应答表单（补料上传 或
     `omit_slot` 单键省略）、交付物下载、运行/取消/恢复按钮。

5. **HTTP 层合规边界**（不可协商）：
   - 默认 `local_only`；`llm_allowed` 必须是任务级显式勾选，不得由配置文件存在性、
     查询参数或请求头推出（对齐 CLI `--data-policy` 语义，D25）。
   - Web 层只经由 `TaskService` / `AppContainer`（`bootstrap.py` 文档明示 "The CLI
     (and later Web) only ever touches the objects built here"）；不直接构造 provider
     客户端、不直接写仓储之外的旁路。
   - `ModelGatewayError` 映射为 4xx，**不重试、不回退**（ModelGateway 契约）。
   - 错误页只展示 OMAS 错误码与程序自身消息；**不把正文/文件字节放进任何 HTTP
     响应、错误页或日志**（I3 / AGENTS.md 硬边界）。
   - **输入框只喂给 Planner 做判断与槽位匹配，不是正文来源**：正文仍只能来自模板
     静态内容或精确 source span（I2）。聊天式界面不得暗示"用户键入的文字会进入
     最终正文"。

6. **阻塞型处理走线程池**：路由处理器声明为同步 `def`，由 Starlette 线程池执行
   （图执行可能耗时数分钟）；Ledger 已是 `check_same_thread=False` + 每连接
   `RLock` 串行化，满足该用法。`AppContainer` 在应用生命周期内单例，关闭时
   `ledger.close()`。

7. **幂等性自然成立**：submit/respond 的 `request_id` / `decision_id` 由 Web 层
   程序生成（`new_id`），浏览器重复点击同一表单会得到同一 `request_id` → 幂等重放
   而非重复创建（同 key 不同 payload 才报 `IDEMPOTENCY_CONFLICT`，HTTP 409）。
   export 每次点击生成新 `request_id`，且输出文件名含 `delivery_id`，
   避免新交付物覆写旧导出文件（export 对"同路径不同字节"直接拒绝）。

8. **为支撑页面新增两个只读仓储查询**：`TaskRepository.list_recent(limit)` 与
   `TemplateVersionRepository.list_recent(limit)`（此前只有主键查找；见
   `storage/repositories.py`）。它们是纯读、短事务，不改变任何写语义。

## 用户界面与红线

界面形态可以像 ChatGPT（输入框 + 上传 + 对话式追问），但**语义是"任务问答"而非
"聊天生成"**：

- 输入框 → `SubmitTask.intent`（用户意图）；
- 上传 → `SubmitTask.materials`；
- 对话轮次 → `awaiting_user` 缺槽追问 + `RespondTask`（补料或省略），
  这一段是 Web 明显优于 CLI 的地方（CLI 需手拼三个参数）。

用户输入永远不会成为最终正文的来源。这条红线由 Renderer 无自由文本入口（I2）
在程序上强制，Web 层只是不绕过它。

## 后果

- 推翻 ADR 0001 的"MVP 只做 CLI"；保留其余全部锁定决策与硬边界不变。
- AGENTS.md 硬边界条目中的"Web（MVP 内）"删除，指向本 ADR。
- 直接依赖表新增 starlette / uvicorn / python-multipart / httpx(dev)，版本以
  `uv.lock` 为准（均为锁内既有包）。
- 新增 `src/omas/web/` 模块与 `tests/unit/web/` 离线测试（TestClient + weekly_report
  fixture，复用 CLI 测试的确定性闭环）。
- 不交付：多用户、鉴权、HTTPS、反向代理配置、SSE/WebSocket 实时推送（v1 用轮询）、
  通用闲聊、模板在线注册（模板仍由 `omas template extract` 注册后再在网页选用）。

## 修订 A（2026-09-30 晚）：对话式控制台

用户验收后要求把表单式两屏升级为 Claude Code / ZCode 风格的对话式界面。修订内容：

1. **新增 JSON API（`omas/web/api.py`）**：会话（`/api/conversations`）、消息即任务
   （POST `/api/conversations/{id}/messages`，multipart：intent + materials）、
   轮询源 `/api/tasks/{id}/feed`（增量事件 + 任务视图）、缺槽应答
   `/api/tasks/{id}/respond`（decision_id 由 awaiting_event_id+动作+材料摘要派生，
   天然幂等）、成品下载 `/api/tasks/{id}/download`（下载前按 delivery 记录验 hash）、
   模板库 `/api/templates`（列表 / precheck / 上传 / meta）。
2. **任务执行移入后台线程**：`TaskRunner` 每任务一线程、独立 AppContainer（独立
   ledger 连接；WAL + busy_timeout 吸收跨连接竞争，同连接访问已由 RLock 串行化）。
   HTTP 请求立即返回 202，前端靠 feed 轮询渲染进度——不阻塞事件循环。
3. **模板上传自动制备（`omas/templates/scaffold.py`）**：上传的 DOCX 只需含
   `{{ 槽位名 }}` 占位符；书签注入、语义 sidecar（语义说明由上传者提供——程序
   不臆断语义，v1.1 §3.1 不变）、静态区域全量自动生成、最小 styles 规范，全部
   经既有 TemplateRegistry 注册（不可变版本）。不支持的结构仍以 findings 拒绝激活。
4. **持久化（migration 002）**：`conversations` / `conversation_messages` /
   `template_meta`。template_meta 仅是展示层元数据（改名/简介）；模板契约与版本
   依旧不可变。
5. **合规边界不变**：输入框仍只喂 `SubmitTask.intent`（页面明示"输入只作为任务
   意图，不会成为正文"）；`llm_allowed` 仍为会话级显式选择；HTTP 层仍只经
   TaskService/AppContainer；错误响应只含错误码与程序自身消息。

旧表单页面保留于 `/`（兼容），新控制台在 `/console`。

## 修订 B（2026-10-01）：意图分流与综合问答

用户要求控制台同时支持直接提问与文档生成，且明确否决“加一个仅对话按钮”的做法——意图识别必须是程序节点自动完成，单一输入框不变。修订内容：

1. **轮次执行器（`omas/web/turns.py`，TurnExecutor）**：POST 消息接口不再同步产生任务，返回 `202 {"status":"turn_started"}`；同会话上一轮未完成时返回 `409 TURN_BUSY`。会话详情新增 `busy` 字段，前端按会话（而非任务）轮询。
2. **意图分流节点（`omas/agents/triage.py`，TriageAgent）**：三分类 `document_task` / `question` / `needs_info`，`needs_info` 必带 clarifying_question。local_only 会话或无模型配置时**不经分流**，直接走文档流水线（零外发不变）。
3. **综合问答（`omas/agents/qa.py`，QAAgent）**：输出 answer + sources；可挂 web_search / fetch_page 工具（`omas/websearch/`，provider=tavily/searxng/generic/**bing**，全部经 ModelGateway 新增 `web_search`/`web_fetch` 端点类别；local_only 仅允许 loopback 自建实例）。每次搜索追加 `note` 消息（“联网搜索：query”），答案追加 `answer` 消息。bing provider 为 HTML 免 key 方案（解析 SERP `b_algo` 自然结果块），须带浏览器 UA 并跟随 302（www→cn.bing.com），且对跳转后最终 URL 重过一次网关；QA 工具失败只回报错误字符串给模型（换词重试或基于已知作答），不因单工具失败打断轮次。已实测：远端模型 API 虽接受 Anthropic 服务端 `web_search_20250305` 工具，但该后端返回结果恒为空（8/8 次查询 `content:[]`），故不依赖它。
4. **消息类型扩展（migration 003）**：conversation_messages.kind 增加 `answer` / `clarify` / `note`；这些消息**不携带 task_id、不进正文溯源链**——问答产物只是会话内容，与 Renderer 无自由文本入口（I2）的边界正交。
5. **前端（console.js）**：单输入框；发送后进入会话轮询模式，`busy` 期间显示“正在思考”指示并禁用输入；answer 渲染为答案气泡（“来源：”段解析为链接列表），clarify 渲染为暖色左边框追问气泡，note 渲染为灰色过程小行；task_started 仍挂任务卡并按 feed 轮询。切会话即停止轮询并恢复输入区。

不变：HTTP 层仍只经 TaskService/AppContainer；正文来源仍只有模板静态内容与精确 span；所有硬边界（I1–I10、T12 local_only 程序级断言）保持。
