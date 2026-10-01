# OMAS 内置模板库（开发工具）

三份通用中文报告模板（工作报告 / 月报 / 调研报告），随 OMAS 发布物提供。
运行时由 [`omas.templates.seed`](../src/omas/templates/seed.py) 消费，
协议与语义见 [ADR 0003](../docs/adr/0003-builtin-template-seeding.md)。

| 模板 | template_id | 样式结构 | 槽位（均为必填） |
| --- | --- | --- | --- |
| 工作报告 | `work-report` | 大标题 + 3 个一级标题 + 2 个二级标题 | `report_title`、`work_summary`、`key_results`、`open_issues`、`next_steps` |
| 月报 | `monthly-report` | 大标题 + 4 个一级标题 + 2 个二级标题 | `report_title`、`monthly_overview`、`key_metrics`、`work_progress`、`risks`、`next_month_plan` |
| 调研报告 | `research-report` | 大标题 + 4 个一级标题 + 2 个二级标题 | `report_title`、`background`、`methodology`、`findings`、`analysis`、`conclusions` |

## 资产与清单位置

- 生成产物（**打包进 wheel 的包内数据**）：`src/omas/templates/builtin/` ——
  `work-report.docx`、`monthly-report.docx`、`research-report.docx`、
  `builtin-templates.json`（展示元数据 + 逐槽位语义）。
- 本目录只有开发工具：`build_builtin_templates.py`（规格即数据）与本说明。

## 重新生成

```bash
export PATH="$HOME/Library/Python/3.9/bin:$HOME/.local/bin:$PATH"
uv run python templates/build_builtin_templates.py
```

生成器先在**临时 OMAS_HOME** 里对每份模板跑真实 `scaffold_and_register`，
`activatable=True` 才允许写盘到包内数据目录。DOCX 走固定 zip 时间戳的确定性
重打包（对齐 D20）：规格未改时重生成的字节逐位一致，播种的内容摘要不变，
不会产生新版本。

## 注册（运行时）

```bash
omas template seed                    # 显式播种到当前 OMAS_HOME（幂等）
omas web                              # 控制台启动时工厂期自动播种
```

两者都走 `scaffold_payloads → TemplateRegistry.ensure_registered`（与 web 上传
表单同一条注册路径）。幂等语义：同 template_id 最新版本四摘要与资产重算全等 ⇒
返回既有版本、不写盘；不一致 ⇒ 注册新版本、旧版本保留（ADR 0003）。

> 历史脚本曾带 `--register URL` 经 HTTP 上传到运行中的控制台；该模式已移除，
> 注册统一收敛到 seed/启动播种，避免两条路径分叉。

## 模板结构约束（保持零 unsupported findings）

修改 `build_builtin_templates.py` 中的模板规格时必须遵守，否则模板不可激活：

- 占位符段落只含一个 run，文本恰好为 `{{ slot_id }}`（单空格）；
- 槽位 id 为 ASCII 标识符（`[A-Za-z_][A-Za-z0-9_]*`）；
- 槽位段落只用 Title/Normal 样式——列表样式带编号，格式门会对槽位段落判 FAIL；
- 不放表格、页眉页脚、分页符、域或批注：这些是不支持文本容器，或与
  scaffold 按正文段落计数生成的 static map 错位。

约束由 ADR 0003 第 6 条固定；违反约束的资产生成器会在预检阶段失败。
