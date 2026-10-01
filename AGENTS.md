# OMAS 开发约定

办公文档装配与质检系统。最高原则：**LLM 负责判断；程序负责状态、契约、渲染、校验和交付。**

需求基线与实施约束（必读）：

- `docs/reference/MASTER-v1.0.md` — 需求基线（技术栈、不变量 I1–I10、T01–T12、DoD）
- `docs/01-技术收敛与详细开发方案.md` — v1.1 实施收敛（模板子集、数据契约、幂等/恢复协议）
- `docs/adr/` — 决策记录；新增默认值写入 ADR 并标注来源
- `docs/dependency-baseline.md` — 锁定版本与兼容性探针结果

## 硬边界（违反即为缺陷）

- 最终正文只能来自模板静态内容或精确 source span；Renderer 无自由文本入口。
- source offset 基于 NFC+LF canonical text 的 Unicode code point，半开区间 `[start, end)`。
- hash / span_handle / task scope 由程序生成与验证，绝不信任模型自报。
- SQLite 不存正文与文件字节，只存 refs 和有限元数据；文件系统是字节母本。
- 未通过 required gate（含 unknown）不得 finalize；unknown 不是 pass。
- 默认 `local_only`；云调用、追踪、遥测一律过 DataPolicyGuard。
- 所有写用户操作幂等；同 key 不同 payload 报 `IDEMPOTENCY_CONFLICT`。
- 已登记 artifact 不可修改；模板重提取产生新版本，不覆盖。
- 不引入：其他 Agent 框架、队列、向量库、ORM/Alembic、PDF/LibreOffice、PPTX/XLSX。
- Web 仅限 `omas web` 本地回环控制台（ADR 0002）：只绑 loopback、默认 local_only、输入框只喂判断不进正文、HTTP 层只经由 TaskService。

## 开发顺序（固定，不跳阶段）

P0 契约与存储 → P1 无 LLM 的 DOCX 闭环 → P2 Assembler → P3 Planner → P4 Graph/恢复 → P5 CLI 验收。

P5 之后：`omas web` 本地回环控制台（ADR 0002），不改变上述阶段的任何契约。

## 常用命令

```bash
export PATH="$HOME/Library/Python/3.9/bin:$HOME/.local/bin:$PATH"
uv sync --frozen          # 安装锁定依赖
uv run ruff check .
uv run mypy               # 配置 packages=["omas"]，检查 src/omas
uv run pytest tests/unit tests/integration tests/invariants   # P4 起增加 tests/faults
uv run pytest tests/test_probes.py -m probe                  # 兼容探针
```

## 测试纪律

- 默认测试禁止联网；模型一律用受控替身（TestModel/FakeModel）。
- 真实本地模型 smoke 单独执行并在报告中如实记录；没有运行就写未运行。
- 不以改测试、删不变量来让检查通过；失败先修实现。
