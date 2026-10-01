"""Broken-template scenarios: each must produce its finding and block
activation (v1.1 §3.2, T19)."""

from __future__ import annotations

import re

from minidocx import rewrite_document
from weekly_report import build_weekly_report_template

from omas.templates import EXTRACTOR_VERSION, build_contract

_PLACEHOLDER_RUN = "<w:t>{{ sales_summary }}</w:t>"


def _contract_for(docx_bytes: bytes):
    fixture = build_weekly_report_template()
    return build_contract(
        docx_bytes=docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
        version=1,
        extractor_version=EXTRACTOR_VERSION,
    )


def _clean_docx() -> bytes:
    return build_weekly_report_template().docx_bytes


def _codes(docx_bytes: bytes) -> set[str]:
    return {f.code for f in _contract_for(docx_bytes).unsupported_findings}


def test_baseline_clean_fixture_has_no_findings() -> None:
    assert _codes(_clean_docx()) == set()


def test_split_run_placeholder_is_rejected() -> None:
    docx = rewrite_document(
        _clean_docx(),
        lambda doc: doc.replace(
            _PLACEHOLDER_RUN,
            "<w:t>{{ sales_</w:t></w:r><w:r><w:t>summary }}</w:t>",
        ),
    )
    codes = _codes(docx)
    assert "PLACEHOLDER_SPLIT_RUN" in codes
    finding = next(
        f for f in _contract_for(docx).unsupported_findings if f.code == "PLACEHOLDER_SPLIT_RUN"
    )
    assert finding.locator.startswith("document:para[")
    assert not _contract_for(docx).is_activatable()


def test_placeholder_mixed_with_other_text_is_rejected() -> None:
    docx = rewrite_document(
        _clean_docx(),
        lambda doc: doc.replace(
            "<w:t>{{ risks }}</w:t>", "<w:t>前缀 {{ risks }} 后缀</w:t>"
        ),
    )
    assert "PLACEHOLDER_NOT_ALONE" in _codes(docx)
    assert not _contract_for(docx).is_activatable()


def test_jinja_control_syntax_is_rejected() -> None:
    docx = rewrite_document(
        _clean_docx(),
        lambda doc: doc.replace(
            "<w:sectPr",
            "<w:p><w:r><w:t>{% for x in y %}loop{% endfor %}</w:t></w:r></w:p><w:sectPr",
        ),
    )
    assert "JINJA_CONTROL_SYNTAX" in _codes(docx)
    assert not _contract_for(docx).is_activatable()


def test_missing_bookmark_anchor_is_rejected() -> None:
    def strip_risks_anchor(doc: str) -> str:
        doc = re.sub(r'<w:bookmarkStart [^>]*w:name="risks"[^>]*/>', "", doc)
        doc = re.sub(r'<w:bookmarkEnd [^>]*w:id="101"[^>]*/>', "", doc)
        return doc

    docx = rewrite_document(_clean_docx(), strip_risks_anchor)
    assert "PLACEHOLDER_MISSING_ANCHOR" in _codes(docx)
    contract = _contract_for(docx)
    risks = contract.slot("risks")
    assert risks is not None and risks.anchor is None
    assert not contract.is_activatable()


def test_duplicate_placeholder_is_rejected() -> None:
    docx = rewrite_document(
        _clean_docx(),
        lambda doc: doc.replace(
            "<w:sectPr",
            "<w:p><w:r><w:t>{{ sales_summary }}</w:t></w:r></w:p><w:sectPr",
        ),
    )
    assert "PLACEHOLDER_DUPLICATE" in _codes(docx)
    assert not _contract_for(docx).is_activatable()


def test_undeclared_placeholder_is_rejected() -> None:
    docx = rewrite_document(
        _clean_docx(),
        lambda doc: doc.replace(
            "<w:sectPr",
            "<w:p><w:r><w:t>{{ surprise }}</w:t></w:r></w:p><w:sectPr",
        ),
    )
    assert "PLACEHOLDER_UNDECLARED" in _codes(docx)
    assert not _contract_for(docx).is_activatable()


def test_rich_text_tag_is_rejected() -> None:
    docx = rewrite_document(
        _clean_docx(),
        lambda doc: doc.replace(
            "<w:sectPr", "<w:p><w:r><w:t>{{r x}}</w:t></w:r></w:p><w:sectPr"
        ),
    )
    assert "JINJA_RICH_TEXT_TAG" in _codes(docx)
    assert not _contract_for(docx).is_activatable()


def test_filter_syntax_is_rejected() -> None:
    docx = rewrite_document(
        _clean_docx(),
        lambda doc: doc.replace(
            "<w:sectPr", "<w:p><w:r><w:t>{{ x | upper }}</w:t></w:r></w:p><w:sectPr"
        ),
    )
    assert "JINJA_FILTER" in _codes(docx)
    assert not _contract_for(docx).is_activatable()


def test_placeholder_in_header_is_rejected() -> None:
    import io
    import zipfile

    from minidocx import build_zip

    source = zipfile.ZipFile(io.BytesIO(_clean_docx()))
    entries = {info.filename: source.read(info.filename) for info in source.infolist()}
    source.close()
    entries["word/header1.xml"] = entries["word/header1.xml"].replace(
        "内部资料 · 请勿外传".encode(), "内部资料 {{ sales_summary }}".encode()
    )
    assert "PLACEHOLDER_IN_HEADER_FOOTER" in _codes(build_zip(entries))


def test_sidecar_schema_version_mismatch_is_reported() -> None:
    fixture = build_weekly_report_template()
    sidecar = dict(fixture.sidecar)
    sidecar["schema_version"] = "9.9"
    contract = build_contract(
        docx_bytes=fixture.docx_bytes,
        sidecar=sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
        version=1,
        extractor_version=EXTRACTOR_VERSION,
    )
    assert "SIDECAR_SCHEMA_VERSION" in {f.code for f in contract.unsupported_findings}
    assert not contract.is_activatable()


def test_invalid_static_region_locator_is_reported() -> None:
    fixture = build_weekly_report_template()
    static_map = {
        "schema_version": "1",
        "regions": [
            {"region_id": "bogus", "part": "document", "paragraph_range": [99, 100]}
        ],
    }
    contract = build_contract(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=static_map,
        template_id="weekly-report",
        version=1,
        extractor_version=EXTRACTOR_VERSION,
    )
    codes = {f.code for f in contract.unsupported_findings}
    assert "STATIC_REGION_LOCATOR_INVALID" in codes
    assert contract.static_regions == ()
    assert not contract.is_activatable()


def test_missing_style_key_is_reported() -> None:
    fixture = build_weekly_report_template()
    contract = build_contract(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec={},  # no body/heading entries
        static_map=fixture.static_map,
        template_id="weekly-report",
        version=1,
        extractor_version=EXTRACTOR_VERSION,
    )
    assert "STYLE_KEY_UNKNOWN" in {f.code for f in contract.unsupported_findings}
    assert not contract.is_activatable()
