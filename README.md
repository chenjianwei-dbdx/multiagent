# OMAS — 办公文档装配与质检系统

单机、单用户、单执行器。LLM 负责判断；**程序**负责状态、契约、渲染、校验和交付。最终正文只允许两种来源：版本化模板静态内容、用户/上游材料的精确 source span。

需求基线见 [Master v1.0](docs/reference/MASTER-v1.0.md)，实施收敛见 [v1.1 方案](docs/01-技术收敛与详细开发方案.md)，决策记录见 [ADR](docs/adr/)（[0001](docs/adr/0001-mvp-boundaries.md)、[0002](docs/adr/0002-web-console.md)、[0003](docs/adr/0003-builtin-template-seeding.md)），锁定版本与探针结论见 [dependency-baseline](docs/dependency-baseline.md)。

## 安装

```bash
# macOS 环境需先导出 uv 路径（见 AGENTS.md）
uv sync --frozen
```

要求 Python 3.12（`.python-version` 已锁定，uv 自动安装）。全部测试离线运行，不需要任何 API key。

## CLI 快速开始

```bash
export OMAS_HOME=~/.omas            # 数据目录（ledger、文件池、checkpoints）

# 1. 注册契约化模板（DOCX + 语义 sidecar）
omas template extract weekly-report.docx \
  --contract contract.json --styles styles.json --static-map static-map.json \
  --template-id weekly-report

# 2. 提交任务（幂等 key = request_id）
omas task submit --template <version_id> --intent "生成本周周报" \
  --material week.md --request-id req-001

# 3. 运行（确定性管线 + 两道来源门禁 + 格式门禁）
omas task run <task_id>
# 缺料时输出：原因、缺槽列表、awaiting_event_id、epoch、respond 示例

# 4. 补料 / 豁免后继续（同 task，epoch+1）
omas task respond <task_id> --decision-id dec-001 --expected-epoch 1 \
  --awaiting-event <event_id> --material risks.md
omas task run <task_id>

# 5. 查看 / 导出（导出只读 committed delivery，绝不覆盖不同内容）
omas task status <task_id> --json
omas task export <task_id> --request-id exp-001 --output report.docx
omas task events <task_id>
omas task cancel <task_id> --request-id can-001
omas task recover <task_id> --request-id rec-001
```

## 架构一页

```
CLI ─ Application Service ─ LangGraph(执行) ─ TaskLedger(SQLite, 事实) + ArtifactStore(文件, 字节)
                    │
     Planner(意图→槽位计划) ─ Assembler(材料→span 绑定提议) ─ BindingService(程序提交 BindingIR)
                    │
     确定性管线: RenderIR → Gate A → docxtpl 渲染 → Gate B(独立 OOXML 提取) → FormatGate → Finalizer
```

- **模型接入（ADR D25）**：`$OMAS_HOME/models.toml` 配置远端 Anthropic 格式端点（provider/model_id/base_url/api_key_env，密钥只从环境变量读）。默认策略仍 `local_only`（确定性 AutoBinder，零外发）；需要 LLM 的任务在 submit 时显式 `--data-policy llm_allowed`，此时 Planner/Assembler/BindingService 真实链路生效，且每次调用留 llm_calls 审计。`omas config` 查看生效配置（不打印密钥）。
- **崩溃恢复**：task_id 即 thread_id，checkpoint 在 `runtime/checkpoints.sqlite3`；所有节点幂等（内容寻址复用），跨进程 resume 已验证最多一份 delivery。

## 测试与检查

```bash
uv run ruff check .
uv run mypy
uv run pytest tests/unit tests/integration tests/invariants tests/faults
uv run pytest tests/test_probes.py -m probe      # 兼容探针
uv run pytest tests/benchmarks                   # 格式门禁 benchmark（gold 标注）
```

验收基线：T01–T24 语义已映射到各测试套件；格式 benchmark 44 案例 precision/recall=1.00、FPR=0、clean unknown=0。

## 支持与不支持（模板子集）

支持：正文普通段落/固定表格单元格内的 `{{ slot }}`（独占段落、单 run、恰一次）；多 span → 多段落；书签锚点；静态页眉页脚与静态表格。
拒绝（注册时 findings，不激活）：`{{r}}`/`{% %}`/filter/表达式、split-run 占位符、动态页眉页脚、域代码、修订/批注/文本框/脚注/OLE/宏/外部关系/altChunk/sdt、占位符混排其他文字。

## 数据目录

```
$OMAS_HOME/
├── ledger.sqlite3          # 业务事实（refs only，无正文）
├── runtime/checkpoints.sqlite3
├── templates/<id>/<version>/
└── tasks/<task_id>/{inbox,canonical,nodes,projections,deliverables}
```

## 限制（如实声明）

- 真实本地模型 smoke 未执行（环境未配置）；Agent 链路以受控替身验证。
- 格式验证为 OOXML 结构级，不承诺分页与像素级视觉一致。
- 动态行表格、图表、图片槽位按 MVP profile 拒绝（v1.1 §2）。
- Web 控制台已实现（`omas web`，ADR 0002）：`/console` 为 Claude Code 风格对话式界面（会话历史侧栏、模板库上传/改名/简介、任务过程逐步显示工具调用），`/` 保留旧表单页；仅绑定本地回环。

## 远端模型配置（Anthropic 格式）

```toml
# $OMAS_HOME/models.toml（或 --models / OMAS_MODELS_TOML 指定）
[model]
provider = "anthropic"                 # Anthropic wire format（官方 API 或兼容中转）
model_id = "你的模型 ID"
base_url = "https://你的端点"           # 官方 API 可省略
api_key_env = "ANTHROPIC_API_KEY"      # 密钥只从该环境变量读取
```

```bash
omas config                                    # 查看生效配置（不打印密钥）
omas task submit ... --data-policy llm_allowed # 显式选择后该任务才发起远端调用
```

local_only 任务（默认）从不构造远端客户端——这是程序级断言（T12），不是提示词承诺。
