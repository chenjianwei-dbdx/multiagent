"""内置模板播种（ADR 0003）：注册、基于内容的幂等、变更升级、坏资产留档。

播种是系统写而非用户写（AGENTS.md I9 只约束 TaskService 用户操作表面），
其幂等语义由 :meth:`TemplateRegistry.ensure_registered` 的四摘要比较实现。
"""

from __future__ import annotations

import json

import pytest
from tests.unit.cli.conftest import docx_with_jinja_control
from tests.unit.conftest import EnvBox

from omas.templates.registry import TemplateRegistry
from omas.templates.seed import (
    BUILTIN_DIR,
    BuiltinSpec,
    load_builtin_specs,
    seed_builtin_templates,
)

EXPECTED_IDS = ("work-report", "monthly-report", "research-report")
_VERSION_FILES = (
    "template.docx",
    "contract.json",
    "styles.json",
    "static-map.json",
    "manifest.json",
)


def _rows(env: EnvBox) -> list[tuple[str, int, str]]:
    """(template_id, version, template_version_id) of every registered row."""
    return [
        (str(row["template_id"]), int(row["version"]), str(row["template_version_id"]))
        for row in env.ledger.connection.execute(
            "SELECT template_id, version, template_version_id FROM template_versions"
            " ORDER BY template_id, version"
        ).fetchall()
    ]


def test_shipped_manifest_matches_files() -> None:
    specs = load_builtin_specs()
    assert tuple(spec.template_id for spec in specs) == EXPECTED_IDS
    shipped = {path.name for path in BUILTIN_DIR.glob("*.docx")}
    assert shipped == {f"{template_id}.docx" for template_id in EXPECTED_IDS}
    for spec in specs:
        assert spec.slot_semantics, f"{spec.template_id} 语义清单为空"
        assert spec.display_name and spec.description


def test_load_specs_rejects_malformed_manifest(
    env: EnvBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped = load_builtin_specs()
    fake = env.home / "fake-builtin"
    fake.mkdir()
    (fake / "work-report.docx").write_bytes(shipped[0].docx)
    manifest = [
        {
            "template_id": "work-report",
            "display_name": "工作报告",
            "description": "ok",
            "docx": "work-report.docx",
            "slot_semantics": {"report_title": "标题"},
        }
    ]
    (fake / "builtin-templates.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    import omas.templates.seed as seed_module

    monkeypatch.setattr(seed_module, "BUILTIN_DIR", fake)
    specs = load_builtin_specs()
    assert [spec.template_id for spec in specs] == ["work-report"]

    (fake / "builtin-templates.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_builtin_specs()

    (fake / "builtin-templates.json").write_text(
        json.dumps([{"template_id": "x"}]), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="manifest"):
        load_builtin_specs()


def test_seed_registers_builtins_activatable(env: EnvBox) -> None:
    results = seed_builtin_templates(store=env.store, ledger=env.ledger)
    assert [result.template_id for result in results] == list(EXPECTED_IDS)
    assert all(
        result.newly_registered and result.activatable and not result.findings
        for result in results
    )
    assert all(result.version == 1 for result in results)
    for result in results:
        version_dir = env.home / "templates" / result.template_id / "1"
        for name in _VERSION_FILES:
            assert (version_dir / name).is_file(), f"{name} 未落盘"
    metas = {row["template_id"]: row for row in env.ledger.template_meta.list()}
    assert metas["work-report"]["display_name"] == "工作报告"
    assert metas["research-report"]["description"]


def test_seed_is_idempotent(env: EnvBox) -> None:
    first = seed_builtin_templates(store=env.store, ledger=env.ledger)
    before = _rows(env)
    second = seed_builtin_templates(store=env.store, ledger=env.ledger)
    assert all(not result.newly_registered for result in second)
    assert [(r.version, r.version_id) for r in second] == [
        (r.version, r.version_id) for r in first
    ]
    assert _rows(env) == before, "重复播种不得写盘或新增 ledger 行"


def test_seed_registers_new_version_when_semantics_change(env: EnvBox) -> None:
    seed_builtin_templates(store=env.store, ledger=env.ledger)
    specs = list(load_builtin_specs())
    shipped = specs[0]
    specs[0] = BuiltinSpec(
        template_id=shipped.template_id,
        display_name=shipped.display_name,
        description=shipped.description,
        docx=shipped.docx,
        slot_semantics={key: f"{value}（修订）" for key, value in shipped.slot_semantics.items()},
    )
    results = seed_builtin_templates(store=env.store, ledger=env.ledger, specs=tuple(specs))
    by_id = {result.template_id: result for result in results}
    assert by_id["work-report"].newly_registered is True
    assert by_id["work-report"].version == 2
    assert by_id["monthly-report"].newly_registered is False
    versions = [version for tid, version, _ in _rows(env) if tid == "work-report"]
    assert versions == [1, 2], "旧版本必须保留（I10 不可变）"


def test_seed_bumps_user_template_registered_under_same_id(env: EnvBox) -> None:
    """用户以同一 template_id 上传过自己的文档时：不覆盖，播种注册为新版本。"""
    from weekly_report import build_weekly_report_template

    fixture = build_weekly_report_template()
    TemplateRegistry(env.store, env.ledger).register(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="work-report",
    )
    results = seed_builtin_templates(store=env.store, ledger=env.ledger)
    work = next(result for result in results if result.template_id == "work-report")
    assert work.version == 2 and work.newly_registered is True
    assert work.activatable is True
    versions = [version for tid, version, _ in _rows(env) if tid == "work-report"]
    assert versions == [1, 2]


def test_seed_broken_asset_registers_but_flagged(env: EnvBox) -> None:
    """不支持结构的资产仍注册留档（可审计），结果明示不可激活（T19 同构）。"""
    specs = list(load_builtin_specs())
    shipped = specs[0]
    specs[0] = BuiltinSpec(
        template_id=shipped.template_id,
        display_name=shipped.display_name,
        description=shipped.description,
        docx=docx_with_jinja_control(shipped.docx),
        slot_semantics=shipped.slot_semantics,
    )
    broken = tuple(specs)
    results = seed_builtin_templates(store=env.store, ledger=env.ledger, specs=broken)
    bad, *good = results
    assert bad.newly_registered is True
    assert bad.activatable is False
    assert any("JINJA_CONTROL_SYNTAX" in finding for finding in bad.findings)
    assert all(result.activatable for result in good)
    # 同一坏资产重复播种同样幂等（findings 也是契约内容的一部分）
    again = seed_builtin_templates(store=env.store, ledger=env.ledger, specs=broken)
    assert all(not result.newly_registered for result in again)
