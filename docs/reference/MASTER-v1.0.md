# 办公域多智能体系统 · Master 开发文档 v1.0

> 日期：2026-09-30  
> 状态：开发基线  
> 依据：方案书 v0.3.2 + 最新框架收敛结论  
> 用途：可直接交给开发 Agent / Coding Agent 执行。除“明确标为后置”的内容外，不再重新讨论架构选型。

---

# 0. 一句话定义

这是一个**办公文档装配与质检系统**。

它不负责创作正文，而是：理解用户要生成什么文档；识别模板需要哪些内容；从用户提供的原料中找到对应内容；调用工具精确取得原料；将原料绑定到模板槽位；由确定性程序生成 DOCX；在交付前验证“正文是否完全来自允许来源、格式是否满足模板要求”。

最高原则：

> **LLM 负责判断；程序负责状态、契约、渲染、校验和交付。**

---

# 1. 已锁定的技术选型

## 1.1 核心技术栈

- Python：3.12 / 3.13
- 包管理：uv
- 外层编排：**LangGraph**
- LLM 专家层：**PydanticAI**
- 数据契约：**Pydantic v2**
- Web：FastAPI（MVP 先 CLI，Web 后接同一 Service API）
- 业务台账：SQLite
- Artifact：本地文件系统
- DOCX：docxtpl + python-docx + jinja2 + lxml
- PDF / LibreOffice：**不进入 MVP 必需依赖**
- PPTX / XLSX：后置

## 1.2 各组件职责

### LangGraph

只负责：节点编排、条件分支、interrupt / resume、checkpoint、流程恢复，以及后续 fan-out / fan-in。

LangGraph **不是业务事实源**。Graph State 中禁止塞 DOCX 字节、完整正文、大块原料、完整 IR 文件和 Artifact 实体内容，只保存轻量引用和流程信息。

### PydanticAI

用于实现 Planner Agent、Assembler Agent，以及 Full 版的 Reviewer / Viz Agent，负责模型调用、工具调用、强类型输出、schema repair、usage/token 元信息采集。

### SQLite TaskLedger

是业务元数据和状态唯一母本，负责 task、artifact、operation、binding、node run、event、template version、LLM call metadata、finalize / deliver 状态。

### 文件池

所有真实字节均落文件。SQLite 中禁止存 DOCX、PNG、PDF、大段正文和原始附件字节。

---

# 2. MVP 范围

## 2.1 MVP 只做

输入：本地 Markdown、粘贴文本、契约化 DOCX 模板。  
输出：DOCX。

核心链路：

```text
ingest
  ↓
bind_template
  ↓
inventory
  ↓
plan
  ↓
assemble_bind
  ↓
gap_check
  ↓
build_render_ir
  ↓
render_docx
  ↓
provenance_gate
  ↓
format_gate
  ↓
finalize
```

required material 缺失：

```text
gap_check
   ↓
LangGraph interrupt
   ↓
awaiting_user
   ↓
用户补料 / 放弃该槽 / 终止
   ↓
resume
```

## 2.2 MVP 明确不做

PPTX、XLSX、自动生成正文、自动改写正文、图表生成、LLM 逻辑审查、自动 revise、批处理、定时任务、多用户、权限系统、Celery/Kafka/Redis Queue、多 worker、PDF 强制交付、LibreOffice 强依赖。

---

# 3. 系统架构

```text
                    ┌─────────────────────┐
                    │      CLI / Web      │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Application Service │
                    └──────────┬──────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────┐
│                      LangGraph                         │
│                                                        │
│ ingest → template → inventory → planner_agent          │
│                               ↓                        │
│                       assembler_agent                  │
│                               ↓                        │
│                          gap_check                     │
│                      ↙              ↘                  │
│                interrupt          build_render_ir      │
│                                      ↓                 │
│                                 render_docx            │
│                                      ↓                 │
│                              provenance_gate           │
│                                      ↓                 │
│                                format_gate             │
│                                      ↓                 │
│                                  finalize              │
└────────────────────────────────────────────────────────┘

       │                    │                     │
       ▼                    ▼                     ▼
  TaskLedger           ArtifactStore       TemplateRegistry
   SQLite                filesystem          filesystem+SQLite
```

---

# 4. 专家职责

## 4.1 Planner Agent

职责：判断目标文档需要哪些语义槽位。

Planner 不负责：找具体原料、读大量原料正文、生成正文、写 DOCX、决定文件路径。

输出：`ContentPlanIR / ir_skeleton`。

```json
{
  "sections": [
    {
      "section_id": "business_overview",
      "slots": [
        {
          "slot_id": "sales_summary",
          "kind": "text_block",
          "required": true,
          "semantic_requirement": "本周销售情况",
          "preferred_source_kind": ["user", "upstream"]
        }
      ]
    }
  ]
}
```

## 4.2 Assembler Agent

Assembler 必须保留为 LLM 专家。

其职责不是“写文章”，而是：

1. 阅读 Planner 给出的 slot requirement；
2. 查看材料索引；
3. 判断哪些原料与 slot 对应；
4. 必要时调用材料工具搜索；
5. 调用精确读取工具取得原料；
6. 为 slot 建立 source binding；
7. 判断 required slot 是否缺料；
8. 输出 `BindingIR / ir_bound`。

Assembler 可以：判断“销售总结来自 material_003”、决定取 material_003 的某个区间、判断一个槽需要多个材料、声明缺料、调用 search/read/resolve 工具。

Assembler 禁止：改写原料、润色原料、补连接词、自己写结论、自己编数字、把不存在的文字塞入 text slot。

---

# 5. IR 设计

禁止构建“大一统 Universal Render IR”。采用两层设计：

```text
ContentPlanIR
      ↓
BindingIR
      ↓
DocxRenderIR
```

未来：

```text
BindingIR
  ├── DocxRenderIR
  ├── PptxRenderIR
  └── XlsxRenderIR
```

## 5.1 Slot

```python
class Slot(BaseModel):
    slot_id: str
    kind: Literal[
        "text_block",
        "data_table",
        "chart",
        "table_asset",
        "image_asset",
    ]
    required: bool
    semantic_requirement: str
    preferred_source_kind: list[
        Literal["user", "upstream", "viz"]
    ]
    depends_on: list[str] = []
```

注意：`missing` **不是 producer**。

## 5.2 Binding

```python
class SourceSpanRef(BaseModel):
    artifact_id: str
    canonical_sha256: str
    start: int
    end: int
    span_sha256: str

class SlotBinding(BaseModel):
    slot_id: str
    producer: Literal["user", "upstream", "viz"]
    binding_status: Literal["bound", "missing", "invalid"]
    source_refs: list[SourceSpanRef] = []
    input_refs: list[str] = []
```

约束：

```text
binding_status == bound   → source_refs 非空
binding_status == missing → source_refs 必须为空
```

---

# 6. TemplateContract 与 StyleSpec

原 FormatSpec 拆成两个实体。

## 6.1 TemplateContract

回答模板“需要什么”。包含 template_id、version、template_sha256、section tree、placeholder、slot_id、required、slot kind、anchor/locator、static text region、unsupported feature findings。

```json
{
  "slot_id": "sales_summary",
  "placeholder": "{{ sales_summary }}",
  "kind": "text_block",
  "required": true
}
```

## 6.2 StyleSpec

回答内容“应该长什么样”。包含 paragraph style、font、font size、bold、alignment、line spacing、indentation、numbering、margins、section properties。

## 6.3 模板版本

模板任何重新提取均创建新版本，禁止覆盖旧版本。任务绑定模板后，`task.template_version` 在该 task 生命周期内不可静默变化。

---

# 7. 原料与 Provenance

## 7.1 入池 canonicalization

所有文本原料入池时，同时生成 canonical text artifact。

MVP canonicalize 规则：Unicode → NFC；CRLF / CR → LF；其他字符不修改。

span offset 永远基于 canonical text，不得基于原文件未经规范化的字符串位置建立 span。

## 7.2 文本来源

最终业务正文只允许：

```text
template_static
source_span
```

MVP 禁止：

```text
llm_generated
computed_text
auto_summary
auto_transition
```

## 7.3 Renderer API 约束

Renderer 不允许接受：

```python
render(slot_id, text="模型自己写的一句话")
```

只允许：

```python
render(
    slot_id=...,
    source_ref=SourceSpanRef(...)
)
```

Renderer 内部自行从 ArtifactStore resolve 出真实文字。

这意味着：即使 Assembler Agent 想偷偷生成文字，也没有 API 可以把自由文本写入 DOCX。该约束必须由类型/API/权限实现，不靠 Prompt。

---

# 8. Provenance Gate

## Gate A：RenderIR 前验证

每一个业务 text node 必须满足：

```text
origin == template_static
OR
origin == source_span
```

如果是 source_span：artifact 必须存在；canonical_sha256 匹配；start/end 合法；实际 substring 的 sha256 必须等于 span_sha256。

任何失败均为 BLOCKER，禁止 render。

## Gate B：DOCX 后验证

DOCX 生成完成后：检查所有 required slot 已成功落位；对关键 placeholder 做结构定位验证；抽取对应 XML/paragraph/table 内容；对比预期绑定文本；未匹配即 BLOCKER。

Gate B 是二次保险。真正的来源安全首先由“Renderer 只接受 SourceRef”保证。

---

# 9. LangGraph State

Graph State 必须小。

```python
class WorkflowState(TypedDict):
    task_id: str
    operation_id: str
    pipeline_profile: str
    epoch: int

    template_version: str | None
    plan_artifact_id: str | None
    binding_artifact_id: str | None
    render_ir_artifact_id: str | None
    docx_artifact_id: str | None

    awaiting_reason: str | None
    last_error_code: str | None
```

禁止放：full_material_text、full_docx_bytes、full_ir_json、image bytes。

---

# 10. LangGraph 与 TaskLedger 的关系

LangGraph checkpoint 表示“流程执行到哪里”；TaskLedger 表示“这个任务事实上发生了什么”。两者不得混淆。

例如 LangGraph 知道 `current node = assembler`，TaskLedger 知道某 artifact 的 hash、created_by、version，以及 slot 到 source span 的绑定。

如果两者冲突：TaskLedger 的业务事实优先，Graph 恢复时执行 reconcile。

---

# 11. Task / Node 状态

Task：

```text
created
running
awaiting_user
parked
completed
failed
cancelled
```

Node：

```text
pending
running
done
failed_final
skipped
corrupt
```

禁止再增加同义状态。

---

# 12. fan-in 与 ready_to_assemble

必须区分：

`all_deps_terminal`：上游节点都已经不再执行，是调度条件。

`ready_to_assemble`：所有 required slot 满足 `binding_status == bound`，或存在显式 user override，是业务条件。

因此 `all_deps_terminal == true` 不意味着 `ready_to_assemble == true`。缺 required material 必须进入 awaiting_user，而不是继续 assemble。

---

# 13. Assembler Material Tools

MVP 给 Assembler 的工具必须少而稳定。

## list_materials

输入：task_id、kind filter。输出 artifact_id、filename、kind、size、summary metadata。禁止返回全文。

## search_materials

输入：task_id、query、kind、top_k。输出 artifact_id、matched locator、small snippet、score。

## read_material

输入：artifact_id、locator/range。输出精确原文，禁止自动总结。

## resolve_span

输入：artifact_id、start、end。输出 `SourceSpanRef + exact_text`。hash 由程序计算，不接受 LLM 自报。

---

# 14. 文件池

```text
OMAS_HOME/
├── ledger.sqlite3
├── runtime/
│   └── checkpoints.sqlite3
├── templates/
│   └── <template_id>/
│       └── <version>/
└── tasks/
    └── <task_id>/
        ├── task_snapshot.json
        ├── inbox/
        ├── canonical/
        ├── nodes/
        │   └── <run_id>/
        │       ├── work/
        │       ├── out/
        │       ├── logs/
        │       └── manifest.json
        ├── projections/
        │   └── pool_index.json
        └── deliverables/
```

规则：inbox 入池后不可修改；artifact 登记后不可修改；新版本创建新文件；deliverables 只有 finalize 能写；Agent 无 deliverables 写权限；节点只能写自己的 run 目录。

---

# 15. SQLite 最小表

MVP 至少需要：

```text
tasks
operations
artifacts
bindings
node_runs
events
template_versions
llm_calls
deliveries
```

`llm_calls` 至少记录：provider、model_id、model_revision（若可得）、prompt_template_digest、system_prompt_digest、tool_schema_version、model_config、input_artifact_refs、raw_output_artifact_id、parsed_output_artifact_id、tokens_in、tokens_out、cost。

---

# 16. 幂等

所有外部用户操作必须有 idempotency key。

submit 使用 `request_id`，重复 request_id 返回已有 task，不重新创建。

respond 使用 `decision_id`，重复 decision_id 返回第一次决策结果。

operation 状态：

```text
intent
committed
abandoned
```

语义：committed 后重复调用不产生第二次业务副作用。

不承诺跨文件系统 + SQLite 的绝对 exactly-once；允许崩溃窗口中的一次物理重复，但最终业务结果以第一份 committed artifact 为准。

---

# 17. Finalize

finalize 必须是程序步骤：

```text
gate pass
   ↓
读取候选 artifact
   ↓
copy 到 deliverables 临时文件
   ↓
sha256
   ↓
atomic promote
   ↓
TaskLedger 写 delivery record
```

所有成员 Agent 的 deliverables 写权限必须为 0。

---

# 18. 文件安全与数据外发

MVP 输入至少检查：文件大小、扩展名/MIME、zip 解包总大小、zip entry 数量、path traversal、symlink、XML 外部实体、DOCX 异常 relationship、宏格式标记、超深 XML。

Data Policy：

```text
local_only
llm_allowed
```

默认 `local_only`。

如果任务要把正文发给外部云 LLM，必须显式 `data_policy = llm_allowed`。否则使用本地模型，或进入 awaiting_user，不得静默外发。

---

# 19. Format Gate

MVP 只做可确定验证，包括：模板存在、placeholder 数量、required slot、paragraph style、font、font size、alignment、margin、numbering 基础检查、section 基础检查。

不确定时返回 `unknown`，禁止把 unknown 当 pass。

---

# 20. MVP 验收

## 20.1 P0 Invariant：必须 100% 通过

- T01 正常组装：完整材料可走到 delivery。
- T02 LLM 偷写文字：Assembler 输出不存在于 source span 的文字，必须无法进入 Renderer。
- T03 source span 被修改：span_sha256 不一致必须 BLOCKER。
- T04 required 缺料：必须 awaiting_user，禁止生成正式 deliverable。
- T05 resume：用户补料后从 interrupt 恢复，不创建第二 task。
- T06 crash recovery：未 committed 操作可重执行，已 committed artifact 不重复交付。
- T07 重复 submit：相同 request_id 只能产生 1 task。
- T08 重复 respond：相同 decision_id 只接受一次。
- T09 template version：任务运行中模板产生新版本，当前任务继续使用旧绑定版本。
- T10 越权路径：节点写其他 node/deliverables 必须拒绝。
- T11 hash corruption：已登记 artifact 被修改必须标 corrupt，不得继续正常交付。
- T12 data policy：`local_only` 原料不得进入外部 LLM adapter，必须程序级断言。

## 20.2 Format Benchmark

建立至少 20 clean cases + 20 injected cases。注入字体、字号、对齐、编号、边距、缩进、样式漂移、placeholder 缺失。

P0 业务不变量不使用“90%”标准，必须 100%；格式检测才统计 recall、precision、false positive rate。

---

# 21. 推荐仓库结构

```text
omas/
├── pyproject.toml
├── README.md
├── config/
│   ├── app.yaml
│   └── models.yaml
├── src/omas/
│   ├── domain/
│   │   ├── task.py
│   │   ├── artifact.py
│   │   ├── ir.py
│   │   ├── binding.py
│   │   ├── template.py
│   │   └── findings.py
│   ├── graph/
│   │   ├── state.py
│   │   ├── build.py
│   │   └── nodes/
│   │       ├── ingest.py
│   │       ├── bind_template.py
│   │       ├── inventory.py
│   │       ├── plan.py
│   │       ├── assemble_bind.py
│   │       ├── gap_check.py
│   │       ├── build_render_ir.py
│   │       ├── render_docx.py
│   │       ├── provenance_gate.py
│   │       ├── format_gate.py
│   │       └── finalize.py
│   ├── agents/
│   │   ├── planner.py
│   │   └── assembler.py
│   ├── tools/
│   │   └── materials.py
│   ├── storage/
│   │   ├── ledger.py
│   │   ├── artifact_store.py
│   │   └── migrations/
│   ├── templates/
│   │   ├── registry.py
│   │   ├── extractor.py
│   │   └── contract_validator.py
│   ├── renderers/
│   │   └── docx_renderer.py
│   ├── gates/
│   │   ├── provenance.py
│   │   └── format.py
│   ├── security/
│   │   ├── file_validator.py
│   │   └── data_policy.py
│   ├── services/
│   │   └── task_service.py
│   ├── cli/
│   │   └── main.py
│   └── web/
│       └── app.py
└── tests/
    ├── unit/
    ├── integration/
    ├── invariants/
    └── fixtures/
```

---

# 22. 开发顺序

## Phase 0：契约先行

先完成 Pydantic domain model、TaskLedger schema、ArtifactStore、SourceSpanRef、TemplateContract、StyleSpec。此阶段不接 LLM。

验收：所有 domain schema 单测通过；artifact 不可变；hash 校验通过。

## Phase 1：确定性 DOCX 闭环

实现：

```text
mock BindingIR
→ DocxRenderIR
→ renderer
→ provenance gate
→ format gate
→ finalize
```

先证明不靠 Agent 也能正确生产文档。

## Phase 2：Assembler Agent

实现 list/search/read/resolve_span 材料工具，加入 Assembler。Agent 只能产生 BindingIR，不允许直接操作 DOCX。

## Phase 3：Planner Agent

加入 Planner：`user intent → ContentPlanIR`，并连接 Planner → Assembler。

## Phase 4：LangGraph

再接 graph、interrupt、resume、checkpoint、crash recovery。不要第一天先写 Graph。

## Phase 5：CLI MVP

支持：

```bash
omas template extract xxx.docx
omas task submit --template weekly-report --material weekly.md
omas task status <task_id>
omas task respond <task_id> ...
omas task export <task_id>
```

CLI 完成验收后再做 Web。

## Phase 6：Web

接口：

```text
POST /tasks
GET  /tasks/{id}
POST /tasks/{id}/respond
POST /tasks/{id}/cancel
GET  /tasks/{id}/events
```

Web 只能调用 Application Service，禁止 Web 直接写 ledger、写 artifact、操作 graph 内部节点。

---

# 23. Full 版后置项

MVP 完成后才允许讨论：Viz Agent、Reviewer Agent、revise、PDF、PPTX、XLSX。

Reviewer 只读，不得修改正文。文字相关 revise 必须人工确认；自动 revise 只能操作纯格式/局部结构；revise 后必须重新 gate。

PDF 确有业务需求后，再接 LibreOffice 26.8.x 及真实 macOS headless 冒烟测试。

PPTX/XLSX 均基于 BindingIR 分别实现 PptxRenderIR / XlsxRenderIR，禁止污染 DocxRenderIR。

---

# 24. 开发硬规则

1. 不得自行把 LangGraph State 改成业务数据库。
2. 不得让 Agent 直接写 deliverables。
3. 不得让 LLM 自由字符串进入 DOCX Renderer。
4. 不得把 `missing` 重新塞回 producer。
5. 不得把 unknown gate 结果当 pass。
6. 不得把正文放进 SQLite。
7. 不得修改已登记 artifact。
8. 不得用 Prompt 代替程序权限校验。
9. 不得为了“通用”提前开发 PPTX/XLSX。
10. 不得为了“多智能体”增加无独占职责的 Agent。
11. 不得让 Planner 与 Assembler 同时拥有同一 Binding 写权。
12. 不得在 MVP 引入 Celery/Kafka/Redis Queue。
13. 不得把外部 LLM 允许访问 `local_only` 内容。
14. 任何重大 contract 修改必须同步更新 invariant tests。

---

# 25. 最终不变量

- I1：每一段最终业务正文都有可验证来源。
- I2：LLM 无法绕过 SourceRef 向文档注入自由文本。
- I3：每个共享业务对象只有一个权威写者。
- I4：SQLite 是业务状态母本，文件系统是字节母本。
- I5：LangGraph 只控制执行，不成为业务事实源。
- I6：required material 缺失时系统暂停，而不是猜。
- I7：Gate 未通过时正式 deliverable 不存在。
- I8：Agent 能力边界由 schema、tool permission、path permission 保证，而不是只靠 prompt。
- I9：任何已交付文件都能追溯到模板版本、原料版本、binding、节点运行和模型调用记录。
- I10：MVP 的成功标准是“闭环可靠”，不是“Agent 数量多”。

---

# 26. Definition of Done

只有同时满足以下条件，MVP 才算完成：

- CLI 可以完成一个真实周报任务；
- 模板可提取并版本化；
- Planner 可输出合法 ContentPlanIR；
- Assembler 可通过工具找到并绑定真实原料；
- required 缺失可以 interrupt；
- 用户补料后可以 resume；
- Renderer 无自由文本入口；
- provenance invariant tests 100%；
- artifact hash corruption 能发现；
- crash 后任务可以恢复；
- submit/respond 幂等；
- format gate 能给出结构化结果；
- finalize 只在 gate 通过后发生；
- deliverable manifest 可追溯完整来源；
- `local_only` 数据不会进入外部模型 adapter；
- P0 invariant suite 全绿。

达到以上条件后，才开始 Web UI / Reviewer / Viz / PDF / PPTX / XLSX。

---

# 27. 给开发 Agent 的启动指令

开发时不要直接开始写 LangGraph。

第一阶段只做：

```text
domain schema
+
SQLite TaskLedger
+
ArtifactStore
+
TemplateContract
+
BindingIR
+
DocxRenderIR
+
deterministic renderer
+
provenance gate
```

先构造一个完全不依赖 LLM 的 fixture：

```text
固定模板
+
固定 BindingIR
+
固定 source span
→
生成 docx
→
provenance gate pass
→
finalize
```

这个闭环通过后，再加入 Assembler Agent；Assembler 通过后，再加入 Planner；最后才接 LangGraph。

原则：

> **先证明文档流水线是正确的，再让 Agent 进入流水线。**
