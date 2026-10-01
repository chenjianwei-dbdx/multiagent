"""Versioned template registration, hash-verified reads, version bumping."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from weekly_report import build_weekly_report_template

from omas.artifacts.store import ArtifactStore
from omas.domain.errors import ArtifactHashMismatchError, ArtifactNotFoundError
from omas.storage.db import Ledger
from omas.templates import (
    CONTRACT_FILE,
    DOCX_FILE,
    MANIFEST_FILE,
    STATIC_MAP_FILE,
    STYLES_FILE,
    TemplateRegistry,
)


@pytest.fixture()
def registry(tmp_path: Path) -> TemplateRegistry:
    store = ArtifactStore(tmp_path / "home")
    ledger = Ledger.open(tmp_path / "home" / "ledger.sqlite3")
    yield TemplateRegistry(store, ledger)
    ledger.close()


def _mutated_header(docx_bytes: bytes, marker: str) -> bytes:
    import io

    source = zipfile.ZipFile(io.BytesIO(docx_bytes))
    entries = {info.filename: source.read(info.filename) for info in source.infolist()}
    source.close()
    entries["word/header1.xml"] = entries["word/header1.xml"].replace(
        "内部资料 · 请勿外传".encode(), marker.encode()
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def test_register_writes_all_five_files_and_ledger_row(
    registry: TemplateRegistry, tmp_path: Path
) -> None:
    fixture = build_weekly_report_template()
    version_id, contract = registry.register(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
    )
    assert contract.version == 1
    assert contract.is_activatable()

    home = tmp_path / "home"
    for name in (DOCX_FILE, CONTRACT_FILE, STYLES_FILE, STATIC_MAP_FILE, MANIFEST_FILE):
        assert (home / "templates" / "weekly-report" / "1" / name).is_file(), name

    row = registry._ledger.template_versions.get(version_id)
    assert row is not None
    assert row.version == 1
    assert row.template_id == "weekly-report"
    assert row.extractor_version == contract.extractor_version


def test_versions_increment_per_template_id(registry: TemplateRegistry) -> None:
    fixture = build_weekly_report_template()
    for marker in ("内部资料 v2", "内部资料 v3"):
        docx = _mutated_header(fixture.docx_bytes, marker)
        registry.register(
            docx_bytes=docx,
            sidecar=fixture.sidecar,
            styles_spec=fixture.styles_spec,
            static_map={**fixture.static_map, "note": marker},
            template_id="weekly-report",
        )
    _version_id, contract = registry.register(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
    )
    assert contract.version == 3  # re-registering the same DOCX -> new version

    # a different template_id starts at 1 again
    _other_id, other = registry.register(
        docx_bytes=fixture.docx_bytes,
        sidecar={**fixture.sidecar, "template_id": "weekly-report-alt"},
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report-alt",
    )
    assert other.version == 1


def test_get_contract_round_trips_identically(registry: TemplateRegistry) -> None:
    fixture = build_weekly_report_template()
    version_id, contract = registry.register(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
    )
    loaded = registry.get_contract(version_id)
    assert loaded == contract


def test_load_docx_verifies_hash(registry: TemplateRegistry) -> None:
    fixture = build_weekly_report_template()
    version_id, _contract = registry.register(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
    )
    assert registry.load_docx(version_id) == fixture.docx_bytes


def test_tampered_docx_fails_hash_verification(
    registry: TemplateRegistry, tmp_path: Path
) -> None:
    fixture = build_weekly_report_template()
    version_id, _contract = registry.register(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
    )
    target = tmp_path / "home" / "templates" / "weekly-report" / "1" / DOCX_FILE
    target.write_bytes(b"tampered")
    with pytest.raises(ArtifactHashMismatchError):
        registry.load_docx(version_id)


def test_unknown_version_id_raises(registry: TemplateRegistry) -> None:
    from omas.domain.ids import TemplateVersionId

    with pytest.raises(ArtifactNotFoundError):
        registry.get_contract(TemplateVersionId("tver_" + "0" * 32))


def test_non_activatable_contract_is_still_registered(registry: TemplateRegistry) -> None:
    fixture = build_weekly_report_template()
    # sidecar references a slot whose placeholder is absent from the docx
    broken_sidecar = {
        "schema_version": "1.1",
        "template_id": "weekly-report",
        "slots": [
            {
                "slot_id": "ghost",
                "placeholder": "{{ ghost }}",
                "kind": "text_block",
                "required": True,
                "allow_user_omit": False,
                "semantic_requirement": "缺失槽位",
                "style_key": "body",
            }
        ],
        "sections": [{"section_id": "ghost-section", "slot_ids": ["ghost"]}],
    }
    version_id, contract = registry.register(
        docx_bytes=fixture.docx_bytes,
        sidecar=broken_sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
    )
    assert not contract.is_activatable()
    assert "PLACEHOLDER_MISSING" in {f.code for f in contract.unsupported_findings}
    loaded = registry.get_contract(version_id)
    assert loaded == contract


def test_version_files_are_immutable(registry: TemplateRegistry) -> None:
    fixture = build_weekly_report_template()
    registry.register(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
    )
    from omas.domain.errors import ArtifactExistsError

    with pytest.raises(ArtifactExistsError):
        registry._store.write_immutable(
            registry._store.template_relative("weekly-report", "1", DOCX_FILE),
            b"overwrite attempt",
        )
