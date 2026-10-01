"""omas template seed：注册内置模板、幂等重跑（ADR 0003）。"""

from __future__ import annotations

from typer.testing import CliRunner

from tests.unit.cli.conftest import Workbench, kv, register_template

_VERSION_FILES = (
    "template.docx",
    "contract.json",
    "styles.json",
    "static-map.json",
    "manifest.json",
)
_EXPECTED = ("work-report", "monthly-report", "research-report")


def _seed(runner: CliRunner, bench: Workbench):
    return bench.cli(runner, "template", "seed")


def test_seed_registers_builtins(runner: CliRunner, bench: Workbench) -> None:
    result = _seed(runner, bench)
    assert result.exit_code == 0, result.output
    for template_id in _EXPECTED:
        assert kv(result.stdout, template_id) == "v1 registered"
        version_dir = bench.home / "templates" / template_id / "1"
        for name in _VERSION_FILES:
            assert (version_dir / name).is_file(), f"{template_id}/{name} 未落盘"
    assert kv(result.stdout, "activatable") == "True"
    assert kv(result.stdout, "version_id").startswith("tver_")


def test_seed_is_idempotent_second_run_reports_up_to_date(
    runner: CliRunner, bench: Workbench
) -> None:
    first = _seed(runner, bench)
    assert first.exit_code == 0, first.output
    second = _seed(runner, bench)
    assert second.exit_code == 0, second.output
    for template_id in _EXPECTED:
        assert kv(second.stdout, template_id) == "v1 up-to-date"
    # 版本号不变：重复播种不产生新版本（每模板首行即 version/动作）
    first_versions = [kv(first.stdout, tid) for tid in _EXPECTED]
    second_versions = [kv(second.stdout, tid) for tid in _EXPECTED]
    assert [v.split()[0] for v in first_versions] == ["v1"] * 3
    assert [v.split()[0] for v in second_versions] == ["v1"] * 3


def test_seed_does_not_touch_user_templates(runner: CliRunner, bench: Workbench) -> None:
    """播种与用户自注册模板互不干扰（I10：不覆盖）。"""
    register_template(runner, bench)
    result = _seed(runner, bench)
    assert result.exit_code == 0, result.output
    assert "weekly-report" not in result.output
    assert kv(result.stdout, "work-report") == "v1 registered"
    # 用户模板仍停在版本 1，内置模板各占自己的 template_id 目录
    assert (bench.home / "templates" / "weekly-report" / "1" / "template.docx").is_file()
