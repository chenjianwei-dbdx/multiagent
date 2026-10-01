"""omas template extract：成功注册 / 版本递增 / 不支持模板退出码 2。"""

from __future__ import annotations

from typer.testing import CliRunner

from tests.unit.cli.conftest import (
    Workbench,
    docx_with_jinja_control,
    kv,
    register_template,
)

_TEMPLATE_FILES = (
    "template.docx",
    "contract.json",
    "styles.json",
    "static-map.json",
    "manifest.json",
)


def _extract(runner: CliRunner, bench: Workbench, docx: str) -> object:
    return bench.cli(
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


def test_extract_success_prints_version_and_persists_files(
    runner: CliRunner, bench: Workbench
) -> None:
    result = _extract(runner, bench, "template.docx")
    assert result.exit_code == 0, result.output
    assert kv(result.stdout, "template_id") == "weekly-report"
    assert kv(result.stdout, "version") == "1"
    assert kv(result.stdout, "activatable") == "True"
    version_id = kv(result.stdout, "version_id")
    assert version_id.startswith("tver_")
    version_dir = bench.home / "templates" / "weekly-report" / "1"
    for name in _TEMPLATE_FILES:
        assert (version_dir / name).is_file(), f"{name} 未落盘"


def test_reextract_creates_new_version_without_overwrite(
    runner: CliRunner, bench: Workbench
) -> None:
    first = register_template(runner, bench)
    result = _extract(runner, bench, "template.docx")
    assert result.exit_code == 0, result.output
    assert kv(result.stdout, "version") == "2"
    second = kv(result.stdout, "version_id")
    assert second.startswith("tver_") and second != first
    for version in ("1", "2"):
        for name in _TEMPLATE_FILES:
            assert (bench.home / "templates" / "weekly-report" / version / name).is_file()


def test_unsupported_template_registers_but_exits_2(
    runner: CliRunner, bench: Workbench
) -> None:
    bad = bench.inputs / "bad.docx"
    bad.write_bytes(docx_with_jinja_control((bench.inputs / "template.docx").read_bytes()))
    result = _extract(runner, bench, "bad.docx")
    assert result.exit_code == 2
    assert "JINJA_CONTROL_SYNTAX" in result.stdout
    assert kv(result.stdout, "activatable") == "False"
    assert kv(result.stdout, "version_id").startswith("tver_")
    assert "TEMPLATE_UNSUPPORTED" in result.stderr
    version_dir = bench.home / "templates" / "weekly-report" / "1"
    for name in _TEMPLATE_FILES:
        assert (version_dir / name).is_file(), "不支持模板也应注册留档（可审计）"


def test_invalid_contract_json_is_a_usage_error(runner: CliRunner, bench: Workbench) -> None:
    broken = bench.inputs / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    result = bench.cli(
        runner,
        "template",
        "extract",
        str(bench.inputs / "template.docx"),
        "--contract",
        str(broken),
        "--template-id",
        "weekly-report",
    )
    assert result.exit_code == 2
    assert "不是合法 JSON" in result.stderr
