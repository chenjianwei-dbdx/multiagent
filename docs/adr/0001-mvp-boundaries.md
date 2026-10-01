# ADR 0001: MVP 边界与 P0 实施决策

日期：2026-09-30
状态：已接受（P0 实施中）

## 背景

[Master v1.0](../reference/MASTER-v1.0.md) 是需求基线；[v1.1 收敛方案](../01-技术收敛与详细开发方案.md) 是实施补充。本 ADR 区分两者：**已锁定决策**来自 Master 原文；**新增默认值**是 v1.1/实施期引入的建议值，标注来源，业务方未确认前按默认执行、可被推翻。

## 已锁定决策（源自 Master，不可单方面更改）

- 技术栈：Python 3.12、uv、Pydantic v2、PydanticAI、LangGraph、sqlite3（无 ORM）、本地文件池、docxtpl/python-docx/Jinja2/lxml、Typer；MVP 只做 CLI，Web 后置。
- 正文来源仅两种：`template_static`、`source_span`；Renderer 无自由文本入口（I2）。
- SQLite 为业务状态母本，文件系统为字节母本（I4）；LangGraph 只控制执行（I5）。
- 状态枚举、九张业务表、T01–T12 验收项、I1–I10 不变量、开发顺序 P0→P5。
- 幂等：request_id / decision_id / operation_key；同 key 不同 payload 冲突。
- 默认 `local_only`；云访问需显式 `llm_allowed`。

## 新增默认值（v1.1 引入，标注来源，业务未确认）

| # | 决策 | 默认值 | 来源 | 备注 |
|---|---|---|---|---|
| D1 | 配置格式 | app.toml / models.toml（TOML）替代 YAML | v1.1 §1.1 | 减少 PyYAML 依赖 |
| D2 | 模板子集 | 占位符独占段落、单 run、每槽一次；拒绝 `{{r}}`/subdoc/表达式/循环/宏 | v1.1 §3.2 | split-run 拒绝并定位 |
| D3 | Markdown 处理 | 按原文装配，标记字符原样保留，不做 MD→Word 转换 | v1.1 §2 | 待业务确认 |
| D4 | slot 枚举收敛 | MVP profile 只启用 text_block 与固定表格单元格文本 | v1.1 §2 | chart/image/动态表格拒绝 |
| D5 | required 豁免 | `allow_user_omit` 默认 false，模板逐槽开启 | v1.1 §12 | 待业务逐槽确认 |
| D6 | span 计数 | Unicode code point，半开区间，NFC+LF canonical | v1.1 §4.1 | 已实现并有属性测试 |
| D7 | 判别联合 | BoundBinding/MissingBinding/InvalidBinding；missing 无 producer | v1.1 §2 | 已实现 |
| D8 | viz producer | DTO 保留枚举值，MVP 提交时拒绝 | v1.1 §4.1 | 扩展点不删 |
| D9 | Gate A | 固定增加 render 前 provenance_precheck | v1.1 §2 | 生产图节点 |
| D10 | unknown 语义 | required check unknown ⇒ overall unknown ⇒ 不可 finalize | v1.1 §2 | 已实现于 GateReport |
| D11 | gate 复用 | 仅同 candidate sha+模板版本+IR sha+rule version 有效 | v1.1 §2 | P1 实现 |
| D12 | DB 附加表 | schema_migrations、user_decisions、slot_overrides、gate_reports、awaiting_events、resolved_spans | v1.1 §4.3 | resolved_spans 因 span_handle 设计 |
| D13 | span_handle | 工具返回程序签发 handle，专家不写 BindingIR 本体 | v1.1 §4.2 | P2 实现 |
| D14 | 单写者锁 | filelock；一个 OMAS_HOME 一个写执行器 | v1.1 §1.1 | 非多 worker 调度 |
| D15 | 限制默认值 | 文本 5MiB、DOCX 20MiB、zip 展开 100MiB、entries 2000、XML 深度 128、材料 50 份 | v1.1 §9 | 工程初值非测得容量 |
| D16 | 预算默认值 | list 50 / top_k 10 / snippet 200 / read 8000 / 每轮 20 / 总 40000 / repair 2 / 超时 120s | v1.1 §6 | P2 落地 |

## P0 实施决策（本阶段引入）

1. **ID 形态**：`<prefix>_<uuid4hex>`，程序生成（`domain/ids.py`），外部输入与模型输出不接受 ID 构造。
2. **时间**：全部 UTC、timezone-aware；SQLite 存 ISO8601 文本。
3. **frozen DTO**：所有 domain 模型 `extra="forbid"` + `frozen=True`。明示：frozen 不是深不可变（容器字段仍可变内容），不可变性最终依赖文件只追加、hash 校验、受控写接口。
4. **连接参数**：`foreign_keys=ON`、`busy_timeout=5000`、WAL、`synchronous=FULL`；不在事务内等待 LLM/渲染。
5. **迁移**：SQL 文件逐版执行并记录 checksum；checksum 不符即报错，禁止启动期改表。
6. **ArtifactRecorder 协议**：文件池（artifacts 模块）通过注入的 recorder 登记 Artifact，与 SQLite 层解耦；由上层（services）组装。
7. **非法控制字符**：`\a`、`\f` 等 XML/docxtpl 危险字符检测后拒绝，不过滤、不静默继续（v1.1 §3.2）。
8. **探针先行**：四个兼容探针（PydanticAI TestModel、LangGraph SQLite 跨进程 resume、docxtpl 特殊字符、OOXML anchors/styles）结果记录于 [dependency-baseline.md](../dependency-baseline.md)，决定 P1 模板子集细节。
9. **Lint 基线**：忽略 RUF001–RUF003（中文全角标点是本项目业务数据）与 UP012（显式 `.encode("utf-8")` 是 UTF-8 契约的一部分，保留意图）。
10. **存储层偏差**：operations 表在规范 DDL 基础上增加 `output_artifact_ids_json` 列，承载 `Operation.output_artifact_ids`——committed 操作的输出 refs 是幂等重放契约（v1.1 §8.1）的核心数据，不能只靠 join 恢复。

## P1–P5 实施决策（后续阶段新增）

| # | 决策 | 内容 | 来源 |
|---|---|---|---|
| D17 | Gate B 比对策略 | 以"模板+IR 展开后的完整期望文档"逐段落比对（锚点定位为辅），天然覆盖 T13 槽外插字；静态区域摘要用提取器同一 token 流格式复算 | P1 实施 |
| D18 | 图内确定性尾部 | 生产图节点名齐全（render_docx→provenance_gate→format_gate→finalize），实现共享 DeterministicRenderPipeline 的内容寻址幂等，重放复用 committed 输出 | P4 实施 |
| D19 | 离线组合根 | 无本地模型配置期间（用户暂缓提供），bootstrap 默认 AutoBinder（Markdown 节→槽的确定性绑定）；Planner/Assembler+BindingService 已就绪，模型配置后接入 | 用户决策 2026-09-30 |
| D20 | fixture 确定性 | python-docx 生成的 DOCX 携 zip 时间戳，跨进程字节不稳；fixture 重打包固定 entry 顺序与时间戳 | P4 故障测试发现 |
| D21 | 渲染器 jinja 配置 | 必须显式传自建 `Environment(undefined=StrictUndefined, autoescape=True)` 再 render(autoescape=True)；只传 autoescape 得不到 StrictUndefined | 探针 3 + 源码核实 |
| D22 | span 正文含 `{{` | 渲染后残留占位符检测会拒绝含字面 `{{` 的原料正文（RenderError），用户需修正原料；不做转义放行 | 渲染器测试固化 |
| D23 | 未声明编号 | FormatGate：spec 未声明 numbering 而槽段落带 numPr 判 FAIL（可见的结构漂移） | benchmark 驱动 |
| D24 | benchmark 语料 | 20 clean + 24 injected（8 类），全部按 gold 分类，precision/recall=1.00、FPR=0、clean unknown=0 | P5 实施 |

| D25 | 远程 Anthropic 格式模型 | 用户 2026-09-30 决定改用远端 Anthropic 格式 API（替代本地模型）。落地边界：默认仍 local_only；仅任务显式 `--data-policy llm_allowed` 时按 task 策略构造网关并发起远端调用（T12 程序级断言保持）；API key 只从环境变量读（models.toml 只写 `api_key_env` 名，密钥不落盘不入库）；local_only 任务零远端构造、走 AutoBinder；每次调用留 llm_calls 审计行（tokens 未知记 null） | 用户决策 2026-09-30 |
| D26 | 意图分流与综合问答 | 用户 2026-09-30 要求控制台同时具备“联网搜索 + 综合问答”，且**不加手动模式开关**：由程序侧 Triage 节点识别意图——`document_task` 才提交装配流水线，`question` 由 QAAgent 直接回答（答案只进会话气泡，不进正文溯源链，I2 不变），`needs_info` 追问用户补充信息。落地边界：local_only 会话不经分流（零远端构造，全部走文档流水线，T12 保持）；仅 llm_allowed 会话做分流；QAAgent 可挂搜索工具（websearch 模块，httpx trust_env=False、2MiB 有界读取、错误只含状态码/类名）；搜索结果只是判断输入，绝不作为正文来源 | 用户决策 2026-09-30 |
| D27 | 联网采集进材料池（调研→组装闭环） | 用户 2026-10-01 要求"选定模板→提需求→自动联网调研→整理→组装"闭环。**核心不变量不降级**：正文仍只能来自模板静态内容或精确 span；扩展的是"材料"的来源——新增图内 research 节点（位于 plan 与 assemble 之间，仅 llm_allowed），LLM **只选 URL**，字节数由程序 `fetch_page` 抓取并经 ResearchService 登记为 canonical 材料（每条来源的 URL/查询词/抓取时间入 `research_sources` 表，migration 004）。研究 digest（模型按槽位整理已登记页面内容的笔记）同样由程序登记为 `program://` 材料，不占网页抓取预算。local_only 不经 research 节点零外发；采集/抓取失败降级为缺槽追问（gap_check 兜底），budget 耗尽不再崩溃整轮；装配器工具预算随材料数自适应放大（20→80 次调用上限 still bounded） | 用户决策 2026-10-01 |

## 待业务确认（不阻塞 P0/P1 fixture）

1. 严格模板子集 + Markdown 按原文显示是否接受（D2/D3）。
2. 哪些 required 槽允许用户豁免（D5）。
3. 真实本地模型运行环境与模型 ID（未配置前真实模型验收标"未验证"）。
4. 表格动态行、分页视觉保证是否后置（当前建议后置）。

## 后果

- P0 交付：工程基架、domain 契约、SQLite 台账、文件池、canonical/span 核心、四探针、离线测试。
- 不交付：Renderer、Gate、Agent、Graph、CLI（按阶段计划后置；不造空壳）。
- 依赖版本以 uv.lock 为准；版本组合的实测结论见 dependency-baseline.md，不在文档中声称未实测的兼容性。
