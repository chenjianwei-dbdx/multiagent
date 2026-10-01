# 依赖基线与兼容性探针结果

日期：2026-09-30。平台：macOS（darwin 25.5.0，arm64）。Python：3.12.14（uv 管理，`.python-version` 锁 3.12）。

精确版本以 `uv.lock` 为准（`uv sync --frozen` 可复现）。本文记录 P0 实测结论；未实测的组合不做兼容性宣称。

## 锁定的直接依赖

| 包 | 锁定版本 | 用途 |
|---|---|---|
| pydantic | 2.13.5 | 数据契约（extra=forbid、判别联合、frozen） |
| pydantic-settings | 2.15.0 | 配置（后置阶段使用） |
| pydantic-ai | 2.52.0 | Planner/Assembler 专家（P2/P3 使用） |
| langgraph | 1.2.12 | 编排（P4 使用） |
| langgraph-checkpoint-sqlite | 3.1.1 | SQLite checkpointer（P4 使用） |
| langgraph-checkpoint | 4.2.0 | （随 langgraph 解析） |
| docxtpl | 0.20.2 | 受控占位符渲染（P1 使用） |
| python-docx | 1.2.0 | DOCX 读取与测试造样 |
| jinja2 | 3.1.6 | docxtpl 模板引擎 + Web 控制台页面模板（ADR 0002） |
| lxml | 6.1.3 | OOXML 检查 |
| typer | 0.27.2 | CLI（P5 使用） |
| filelock | 4.0.7 | 单写执行器锁 |
| starlette | 1.7.0 | `omas web` 本地回环控制台（ADR 0002；锁内既有包提升为直接依赖） |
| uvicorn | 0.54.0 | 同上（ASGI server） |
| python-multipart | 0.0.32 | 同上（multipart 文件上传解析） |
| pytest / hypothesis / ruff / mypy | 9.1.1 / 6.168.3 / 0.16.9 / 2.3.1 | 开发组 |
| httpx | 0.28.1 | 开发组：starlette TestClient（Web 层离线测试） |

## ADR 0002 依赖说明（2026-09-30 实测）

- Web 控制台**未引入任何未锁定包**：fastapi 不在锁内（实测本环境 PyPI 访问极慢，
  8 秒仅传 15KB，全新依赖不可行）；starlette / uvicorn / python-multipart /
  httpx 已在锁内（原为 pydantic-ai→mcp 的传递依赖），提升为直接依赖。
- `uv lock --offline` 25ms 完成重解析，diff 仅 4 行新增（omas 依赖与 dev 组），
  无任何既有包版本漂移；`uv sync --frozen` 正常。
- Starlette 1.7.0 + uvicorn 0.54.0 + python-multipart 0.0.32 导入与
  `starlette.testclient.TestClient`（基于 httpx）实测可用。
- Web 层路由处理器为同步 `def`（Starlette 线程池执行阻塞型图执行）；Ledger
  为 `check_same_thread=False` + 每连接 `RLock` 串行化，满足该用法。

## 兼容性探针（probes/，全部离线、全部通过）

运行方式：`uv run python probes/probeN_*.py`；pytest 侧：`uv run pytest tests/test_probes.py -m probe`（4 passed）。实测退出码均为 0。

### 探针 1：PydanticAI 受控模型与结构化输出（pydantic-ai 2.52.0）

- `TestModel` 完全离线可用，无需 API key；自动调用工具（按参数名生成占位入参）。
- frozen + `extra="forbid"` DTO 实测生效（多余字段被拒）。
- **与旧文档不符**：`result.usage` 是属性而非方法（调用会抛 TypeError）；TestModel 的 usage 是合成的非零值（实测 input_tokens=108、output_tokens=17、requests=2、tool_calls=1、cost=None）——usage 记账逻辑不能假设 0/None。
- 重试参数为 `Agent(retries=N)`（旧名 `output_retries` 在 2.x 已移除）；retries=1 实测最多 2 次请求后成功，retries=0 抛 `UnexpectedModelBehavior`。
- 结构化输出经名为 `final_result`（多类型时 `final_result_<TypeName>`）的 output tool call 交付。

### 探针 2：LangGraph + SQLite 跨进程 interrupt/resume（langgraph 1.2.12 + SqliteSaver 3.1.1）

- 同步 `SqliteSaver(sqlite3.Connection(...))` 即可用，无需 asyncio。
- **真实跨进程 resume 成功**（父/子进程 pid 不同，同一 checkpoint 文件与 thread_id）。
- **节点重执行实测**：resume 后中断节点从头重放（node_a 执行 2 次），`interrupt()` 之后的代码只执行 1 次；interrupt 之前对 state 的写入不提交 → P4 节点必须幂等（与 v1.1 §8 一致）。
- 中断时 `invoke` 返回含 `__interrupt__`；`get_state().next == ('node_a',)`——中断节点仍 pending，不能以 `next` 为空判断完成。

### 探针 3：docxtpl 特殊字符与空白保真（docxtpl 0.20.2）— 对 P1 最关键

- **与旧预期相反**：LF `\n` 会自动转为 `<w:br/>`、Tab `\t` 会自动转为 `<w:tab/>`（`resolve_listing()` 每次渲染后无条件替换，docxtpl/template.py L383-395）。反向抽取必须把 w:br→`\n`、w:tab→`\t`，只拼 `w:t` 文本会丢失（实测读回只剩 `'ab'`）。
- 额外：`\a`(0x07) 拆分段落；`\f` 变 `<w:br w:type="page"/>`（读回降级为 `\n`）→ 印证 P0 决策：这两类控制字符在入池校验时直接拒绝（domain `find_illegal_control_chars`）。
- **autoescape=False 时 `<>&` 静默损坏且不报错**（docxtpl 内部 `XMLParser(recover=True)` 吞错，L520-521；实测读回只剩 `'a'`）→ P1 渲染必须 autoescape=True；开启后 `<>&"'` 全部完整保真。
- 中文（含全角标点）、emoji（含 ZWJ 组合 👨‍👩‍👧‍👦）、连续/首尾空格（`xml:space="preserve"`）在两种模式下均逐码位完整保留。

### 探针 4：OOXML 锚点与样式读取（python-docx 1.2.0 + lxml 6.1.3）

- 五条读取路径可行：bookmarkStart/End 枚举、w:t/w:tab/w:br 结构化枚举、numbering（numId→abstractNumId→lvl）、有效样式合并（direct→basedOn 链→docDefaults）、页眉页脚 part 枚举（zip 内 `word/header*.xml`）。
- **必须直接用 lxml**：书签注入/编号解析/样式链合并，python-docx 无对应公开 API（实测检查均为 False）。
- direct formatting 解析正确（Arial/SimSun/28 半磅）；`run.font.size` 高层 API 只看 direct（返回 None），字号等有效值必须自行沿样式链解析——P1 FormatGate 按此实现。
- **主题字体**：默认模板 docDefaults 的 `w:rFonts` 只有 `asciiTheme/eastAsiaTheme` 间接引用、无字面 `w:ascii`（读属性得 None）→ P1 字体解析必须处理 `*Theme` 属性，否则全部误报 unknown。

## 对后续阶段的直接影响

1. P1 Renderer：autoescape=True 默认；LF/Tab 依赖 docxtpl 自动转换；Gate B 的独立 extractor 必须按 w:br/w:tab 反向映射（不能用 python-docx 段落 text 拼接）。
2. P1 FormatGate：样式解析走 lxml 直读 + basedOn 链 + docDefaults + 主题属性处理。
3. P2 ModelGateway：usage 经属性读取；cost 为 None 时记 null（v1.1 §4.3），不得假设 0。
4. P4 Graph：所有节点幂等（interrupt 重放实测确认）；不能以 `next==()` 判定 resume 完成。
