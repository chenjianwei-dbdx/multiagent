"""The weekly-report fixture must yield a clean, activatable contract."""

from __future__ import annotations

import hashlib

from weekly_report import build_weekly_report_template

from omas.docx import StyleResolver, extract_docx, paragraph_plain_text
from omas.templates import EXTRACTOR_VERSION, build_contract, canonical_json


def _build() -> tuple:
    fixture = build_weekly_report_template()
    contract = build_contract(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
        version=1,
        extractor_version=EXTRACTOR_VERSION,
    )
    return fixture, contract


def test_fixture_extraction_is_clean() -> None:
    _fixture, contract = _build()
    assert contract.unsupported_findings == ()
    assert contract.is_activatable()


def test_fixture_docx_is_deterministic() -> None:
    first = build_weekly_report_template()
    second = build_weekly_report_template()
    assert hashlib.sha256(first.docx_bytes).hexdigest() == hashlib.sha256(
        second.docx_bytes
    ).hexdigest()


def test_contract_is_deterministic() -> None:
    _, first = _build()
    _, second = _build()
    assert first == second
    assert first.hashes.contract_sha256 == second.hashes.contract_sha256


def test_slot_set_and_sections() -> None:
    _, contract = _build()
    assert contract.slot_ids() == ("sales_summary", "risks", "next_plan")
    assert [s.section_id for s in contract.sections] == ["sales", "risks", "next_plan"]
    risks = contract.slot("risks")
    assert risks is not None and risks.allow_user_omit is True
    sales = contract.slot("sales_summary")
    assert sales is not None and sales.allow_user_omit is False
    assert sales.kind == "text_block"


def test_anchors_are_complete_and_ordered() -> None:
    _, contract = _build()
    anchors = {slot.slot_id: slot.anchor for slot in contract.slots}
    assert all(anchor is not None for anchor in anchors.values())
    bookmark_ids = {anchor.bookmark_id for anchor in anchors.values() if anchor}
    assert bookmark_ids == {"100", "101", "102"}
    orders = sorted(
        (anchor.slot_order, slot_id) for slot_id, anchor in anchors.items() if anchor
    )
    assert [slot_id for _order, slot_id in orders] == [
        "sales_summary",
        "risks",
        "next_plan",
    ]
    sales_anchor = anchors["sales_summary"]
    assert sales_anchor is not None
    assert sales_anchor.part == "document"
    assert sales_anchor.in_table is False
    assert len(sales_anchor.structure_signature) == 64


def test_static_regions_cover_all_static_areas() -> None:
    _, contract = _build()
    regions = {r.region_id: r for r in contract.static_regions}
    assert set(regions) == {
        "title",
        "meta-bullets",
        "heading-sales",
        "heading-risks",
        "heading-plan",
        "classification-table",
        "header-banner",
    }
    table = regions["classification-table"]
    assert table.token_count == 4  # one text token per cell
    header = regions["header-banner"]
    assert header.part == "header"
    assert header.locator.startswith("header1")


def test_package_hashes_are_consistent() -> None:
    fixture, contract = _build()
    assert contract.hashes.docx_sha256 == hashlib.sha256(fixture.docx_bytes).hexdigest()
    assert contract.hashes.styles_sha256 == hashlib.sha256(
        canonical_json(fixture.styles_spec)
    ).hexdigest()
    assert contract.hashes.static_map_sha256 == hashlib.sha256(
        canonical_json(fixture.static_map)
    ).hexdigest()
    payload = contract.model_dump(mode="json")
    payload["hashes"].pop("contract_sha256")
    assert contract.hashes.contract_sha256 == hashlib.sha256(
        canonical_json(payload)
    ).hexdigest()


def test_body_slots_resolve_to_explicit_normal_fonts() -> None:
    fixture, _ = _build()
    resolver = StyleResolver(fixture.docx_bytes)
    doc = extract_docx(fixture.docx_bytes)
    part = doc.part("document")
    assert part is not None
    slot_index = next(
        i
        for i, paragraph in enumerate(part.paragraphs)
        if paragraph_plain_text(paragraph) == "{{ sales_summary }}"
    )
    resolved = resolver.resolve_paragraph(part, slot_index)
    assert resolved.paragraph_style == "Normal"
    assert resolved.font_latin == "Calibri"  # explicit literal, not theme
    assert resolved.font_east_asia == "SimSun"
    assert resolved.font_size_half_points == 21
