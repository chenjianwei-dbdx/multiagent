"""Effective style resolution: direct → pPr → basedOn chain → docDefaults."""

from __future__ import annotations

import io

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt
from minidocx import NUMBERING_TEMPLATE, STYLES_TEMPLATE, build_docx, para

from omas.docx import StyleResolver, extract_docx

DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DIRECT_RPR = (
    f'<w:rPr xmlns:w="{DOCX_NS}"><w:rFonts w:ascii="Arial" w:eastAsia="SimHei"/>'
    f'<w:sz w:val="30"/><w:b/></w:rPr>'
).encode()


def _resolver_for(docx: bytes) -> tuple[StyleResolver, str]:
    resolver = StyleResolver(docx)
    return resolver, "document"


def test_direct_run_formatting_wins() -> None:
    doc = Document()
    paragraph = doc.add_paragraph()
    run = paragraph.add_run("direct")
    run.font.name = "Arial"
    run.font.size = Pt(14)
    run.font.bold = True
    rpr = run._r.get_or_add_rPr()
    rpr.get_or_add_rFonts().set(qn("w:eastAsia"), "SimHei")
    buffer = io.BytesIO()
    doc.save(buffer)

    resolver, part_name = _resolver_for(buffer.getvalue())
    part = extract_docx(buffer.getvalue()).part(part_name)
    resolved = resolver.resolve_paragraph(part, 0)
    assert resolved.font_latin == "Arial"
    assert resolved.font_east_asia == "SimHei"
    assert resolved.font_size_half_points == 28
    assert resolved.bold is True


def test_style_chain_and_doc_defaults_with_minimal_package() -> None:
    body = (
        para("plain")                      # Normal + docDefaults
        + para("body", style="Body")       # Body -> Normal chain
        + para("centered", style="Centered")
        + para(
            "num",
            extra_ppr='<w:numPr><w:ilvl w:val="1"/><w:numId w:val="3"/></w:numPr>',
        )
        + para(
            "broken-num",
            extra_ppr='<w:numPr><w:numId w:val="99"/></w:numPr>',
        )
    )
    docx = build_docx(body, styles=STYLES_TEMPLATE, numbering=NUMBERING_TEMPLATE)
    resolver = StyleResolver(docx)
    part = extract_docx(docx).part("document")

    plain = resolver.resolve_paragraph(part, 0)
    assert plain.font_latin is None  # docDefaults is theme-only -> unknown
    assert plain.font_east_asia is None
    assert plain.font_size_half_points == 22  # docDefaults w:sz
    assert plain.paragraph_style == "Normal"
    assert any("asciiTheme" in note for note in plain.resolution_notes)

    body_style = resolver.resolve_paragraph(part, 1)
    assert body_style.font_latin == "Cambria"  # Body style literal
    assert body_style.font_size_half_points == 24  # Body style w:sz
    assert body_style.font_east_asia is None  # theme-only below Body in chain

    centered = resolver.resolve_paragraph(part, 2)
    assert centered.alignment == "center"  # Centered -> Body chain pPr
    assert centered.font_latin == "Cambria"  # inherited through basedOn

    numbered = resolver.resolve_paragraph(part, 3)
    assert numbered.numbering_id == "3"
    assert numbered.numbering_level == 1

    broken = resolver.resolve_paragraph(part, 4)
    assert broken.numbering_id is None
    assert broken.numbering_level is None
    assert any("numId" in note for note in broken.resolution_notes)


def test_numbering_without_numbering_part_is_unknown() -> None:
    body = para("num", extra_ppr='<w:numPr><w:numId w:val="3"/></w:numPr>')
    docx = build_docx(body, styles=STYLES_TEMPLATE, numbering=None)
    resolver = StyleResolver(docx)
    part = extract_docx(docx).part("document")
    resolved = resolver.resolve_paragraph(part, 0)
    assert resolved.numbering_id is None
    assert any("numbering.xml" in note for note in resolved.resolution_notes)


def test_num_id_zero_disables_numbering() -> None:
    body = para("num", extra_ppr='<w:numPr><w:numId w:val="0"/></w:numPr>')
    docx = build_docx(body, styles=STYLES_TEMPLATE, numbering=NUMBERING_TEMPLATE)
    resolver = StyleResolver(docx)
    part = extract_docx(docx).part("document")
    resolved = resolver.resolve_paragraph(part, 0)
    assert resolved.numbering_id is None
    assert resolved.numbering_level is None


def test_style_level_numbering_via_python_docx_list_bullet() -> None:
    doc = Document()
    doc.add_paragraph("bullet", style="List Bullet")
    buffer = io.BytesIO()
    doc.save(buffer)
    resolver = StyleResolver(buffer.getvalue())
    part = extract_docx(buffer.getvalue()).part("document")
    resolved = resolver.resolve_paragraph(part, 0)
    assert resolved.paragraph_style == "ListBullet"
    assert resolved.numbering_id == "1"
    assert resolved.numbering_level == 0


def test_theme_only_style_font_is_unknown_not_guessed() -> None:
    doc = Document()
    doc.add_paragraph("title", style="Title")
    buffer = io.BytesIO()
    doc.save(buffer)
    resolver = StyleResolver(buffer.getvalue())
    part = extract_docx(buffer.getvalue()).part("document")
    resolved = resolver.resolve_paragraph(part, 0)
    assert resolved.font_latin is None
    assert resolved.font_east_asia is None
    assert any("Theme" in note for note in resolved.resolution_notes)
    assert resolved.font_size_half_points is not None  # size still resolves


def test_basedOn_chain_through_python_docx_heading() -> None:
    doc = Document()
    doc.add_paragraph("heading", style="Heading 2")
    buffer = io.BytesIO()
    doc.save(buffer)
    resolver = StyleResolver(buffer.getvalue())
    part = extract_docx(buffer.getvalue()).part("document")
    resolved = resolver.resolve_paragraph(part, 0)
    assert resolved.paragraph_style == "Heading2"
    assert resolved.font_size_half_points == 26
    assert resolved.bold is True


def test_resolve_paragraph_by_id_with_direct_fragment() -> None:
    doc = Document()
    buffer = io.BytesIO()
    doc.save(buffer)
    resolver = StyleResolver(buffer.getvalue())
    resolved = resolver.resolve_paragraph_by_id("Heading2", DIRECT_RPR)
    assert resolved.font_size_half_points == 30  # direct fragment overrides chain
    assert resolved.font_latin == "Arial"
    assert resolved.paragraph_style == "Heading2"
    # without a fragment the chain value applies
    chained = resolver.resolve_paragraph_by_id("Heading2")
    assert chained.font_size_half_points == 26


def test_out_of_range_paragraph_raises() -> None:
    import pytest

    doc = Document()
    doc.add_paragraph("only")
    buffer = io.BytesIO()
    doc.save(buffer)
    resolver = StyleResolver(buffer.getvalue())
    part = extract_docx(buffer.getvalue()).part("document")
    with pytest.raises(IndexError):
        resolver.resolve_paragraph(part, 5)
