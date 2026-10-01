# ADR 0003: 内置模板播种（Built-in Template Seeding）

日期：2026-09-30
状态：已接受（用户决策 2026-09-30）
关联：ADR 0001（I9/I10、D17–D20）、ADR 0002（`omas web` 启动路径）、
[v1.1 收敛方案 §3](../01-技术收敛与详细开发方案.md)（模板契约与 sidecar）、
[Master §6.3](../reference/MASTER-v1.0.md)（重提取必出新版本）

## 背景

P5 与 ADR 0002 之后，模板进入系统的唯一途径是用户上传 DOCX（`omas template
extract` 或控制台上传表单）。新用户的模板库因此是空的：先用 python-docx 做一份
合规 DOCX、再登记 sidecar 语义，门槛远高于"直接选一份通用周报/月报模板"。

`omas web` 控制台的模板下拉、任务提交链路都依赖"库里有可用模板"这一前提；
空库让控制台首屏即不可用。用户要求内置模板库随发布物提供（工作报告 / 月报 /
调研报告三份通用模板，含大标题与一二级标题）。

Master / v1.1 均未规定内置模板或播种机制，本 ADR 补上这一条默认值。

## 决策

1. **三份通用模板随包发布**（工作报告 / 月报 / 调研报告，`template_id`：
   `work-report` / `monthly-report` / `research-report`）。资产是包内数据：
   `src/omas/templates/builtin/`（`builtin-templates.json` 清单 + 三份 DOCX），
   hatchling `packages = ["src/omas"]` 原样打入 wheel（与
   `storage/migrations/*.sql`、`web/templates/*.html` 同机制，零配置）。
   资产生成器保留在仓库根 `templates/build_builtin_templates.py`（开发工具，
   不进包）：规格即数据，重生成走固定 zip 时间戳重打包（对齐 D20 确定性），
   内容未变时字节逐位一致。

2. **新增播种入口 `omas template seed`**（CLI）与 **`omas web` 工厂期自动播种**
   （`create_web_app` 在 `build_app` 之后、路由注册之前）。自动播种同步执行、
   容器在手，符合 ADR 0002 工厂期 fail-fast 哲学；播种失败只记告警、不阻断
   启动（CLI 命令不吞错，原样上抛）。播种零 LLM、零网络、local_only。

3. **播种经由 scaffold 的既有路径**（`scaffold_payloads` →
   `TemplateRegistry.ensure_registered`），与 web 上传表单共用同一段 payload
   构造——内置模板与用户上传模板在契约层**完全同构**，数据层不做 builtin
   标记、不加 origin 列、不改 schema。

4. **幂等语义（本 ADR 的核心）**：模板注册是**系统写**而非用户写，不参与
   TaskService 的 `IDEMPOTENCY_CONFLICT` 协议（AGENTS.md I9 只约束"写用户
   操作"，`services/task_service.py` 的命令表面）。播种的幂等是**基于内容**的：
   - 同一 `template_id` 最新版本的四摘要（docx / contract / styles /
     static_map）与本次基于资产重算的结果全等 ⇒ 返回既有 `version_id`，
     **任何文件不写盘**（`write_immutable` 本就不会覆盖，I10）；
   - 任何不一致（资产升级、extractor 版本变化、用户以同名 id 注册过自己的
     内容）⇒ 注册**新版本**，旧版本全部保留、可审计。

   实现要点：`build_contract` 是确定性的，以 `version=latest.version` 重建
   探针契约即可与 ledger 行的摘要逐项比对；跳过分支经 `get_contract` 做哈希
   校验读回，顺带复核池文件完整性（I7：文件系统是字节母本）。

5. **展示元数据随版本走**：`template_meta` 仅在实际注册新版本时 upsert。
   控制台的重命名（meta API）因此能在同版本资产上跨重启存活——meta 可变、
   版本不可变（I10 的边界正好落在这里）。

6. **结构约束作为不变量维护**：内置模板必须保持零 unsupported findings、
   activatable=True（槽位段落单 run 独占 `{{ slot }}`、ASCII 槽位名、只用
   Title/Normal 样式、无表格/页眉页脚/域/分页符）。资产生成器在写盘前跑
   真实 `scaffold_and_register` 预检（临时 HOME），非 activatable 直接构建失败；
   逻辑见 `templates/README.md`。坏资产若万一随包发布：仍注册留档（可审计，
   与 `oma template extract` 同构），`omas template seed` 以退出码 2 明示，
   web 启动记告警并继续。

## 后果

- 首次 `omas web` 启动（或首次 `omas template seed`）向 OMAS_HOME 写入 3 个
  模板版本（v1）及其 pool 文件；此后每次重启零写盘、零新版本。
- 包升级导致资产变化时，下次启动自动注册 v2（内同内容、新版本），旧任务
  仍绑定其提交时的 `template_version_id`，不受影响（Master §6.3）。
- 用户以同名 `template_id` 上传过自己的模板时，播种结果为"内置版本成为最新"，
  用户版本保留在历史中——这是可接受的覆盖语义（播种只做注册，不做删除/改写）。
- 修改内置模板必须同时更新生成器规格并重跑生成器（含预检）；直接改
  `src/omas/templates/builtin/` 里的二进制会让重算摘要与发布物不一致，
  属于未走流程的修改。
