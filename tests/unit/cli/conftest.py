"""Shared fixtures/helpers for CLI tests（全部离线，typer.testing.CliRunner）。

sys.path 处理参考 tests/unit/templates/conftest.py：tests/fixtures 不是包，
直接把目录挂到 sys.path 后导入 weekly_report fixture。
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from weekly_report import build_weekly_report_template  # noqa: E402

from omas.cli.main import app  # noqa: E402

MATERIAL_PARTIAL = (
    "# 项目周报材料\n\n## 一、本周销售情况\n\n销售额 500 万，符合预期。\n\n"
    "## 三、下周计划\n\n推进验收。\n"
)
MATERIAL_RISKS = "## 二、风险与依赖\n\n接口联调存在一天延期风险，已登记。"


@dataclass(frozen=True)
class Workbench:
    """一个临时 OMAS_HOME + 预生成的模板/材料输入文件。"""

    home: Path
    inputs: Path

    def cli(self, runner: CliRunner, *args: str) -> Result:
        return runner.invoke(app, ["--home", str(self.home), *args])


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def bench(tmp_path: Path) -> Workbench:
    weekly = build_weekly_report_template()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "template.docx").write_bytes(weekly.docx_bytes)
    for name, payload in (
        ("contract.json", weekly.sidecar),
        ("styles.json", weekly.styles_spec),
        ("static-map.json", weekly.static_map),
    ):
        (inputs / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (inputs / "full.md").write_text(weekly.material_markdown, encoding="utf-8")
    (inputs / "partial.md").write_text(MATERIAL_PARTIAL, encoding="utf-8")
    (inputs / "risks.md").write_text(MATERIAL_RISKS, encoding="utf-8")
    return Workbench(home=tmp_path / "home", inputs=inputs)


def kv(output: str, key: str) -> str:
    """从 `key: value` 行输出里取值。"""
    for line in output.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"key {key!r} not found in output:\n{output}")


def register_template(runner: CliRunner, bench: Workbench, *, docx: str = "template.docx") -> str:
    result = bench.cli(
        runner,
        "template",
        "extract",
        str(bench.inputs / docx),
        "--contract",
        str(bench.inputs / "contract.json"),
        "--styles",
        str(bench.inputs / "styles.json"),
        "--static-map",
        str(bench.inputs / "static-map.json"),
        "--template-id",
        "weekly-report",
    )
    assert result.exit_code == 0, result.output
    return kv(result.stdout, "version_id")


def submit_task(
    runner: CliRunner,
    bench: Workbench,
    *,
    template_version_id: str,
    material: str = "full.md",
    request_id: str = "req-1",
    intent: str = "生成周报",
) -> Result:
    return bench.cli(
        runner,
        "task",
        "submit",
        "--template",
        template_version_id,
        "--intent",
        intent,
        "--material",
        str(bench.inputs / material),
        "--request-id",
        request_id,
    )


def run_task(runner: CliRunner, bench: Workbench, task_id: str) -> Result:
    return bench.cli(runner, "task", "run", task_id)


def docx_with_jinja_control(docx_bytes: bytes) -> bytes:
    """往 document.xml 里插入 {% for %} 控制语法（同 test_broken_templates 做法）。"""
    source = zipfile.ZipFile(io.BytesIO(docx_bytes))
    try:
        entries = {info.filename: source.read(info.filename) for info in source.infolist()}
    finally:
        source.close()
    document = entries["word/document.xml"].decode("utf-8")
    assert "<w:sectPr" in document
    document = document.replace(
        "<w:sectPr",
        "<w:p><w:r><w:t>{% for x in y %}loop{% endfor %}</w:t></w:r></w:p><w:sectPr",
        1,
    )
    entries["word/document.xml"] = document.encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in entries.items():
            target.writestr(name, data)
    return buffer.getvalue()
