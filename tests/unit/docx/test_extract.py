"""Token/structure extraction: w:t / w:tab / w:br, tables, bookmarks, parts."""

from __future__ import annotations

import io

import pytest
from docx import Document
from docx.oxml.ns import qn
from minidocx import build_docx

from omas.docx import document_plain_text
from omas.docx.extract import (
    BREAK,
    TAB,
    TEXT,
    extract_docx,
    paragraph_plain_text,
)


def _sample_docx() -> bytes:
    doc = Document()
    doc.add_paragraph("plain text", style="Heading 1")
    # tab + break inside one run: python-docx maps \t -> w:tab, \n -> w:br
    doc.add_paragraph().add_run("a\tb\nc")
    anchored = doc.add_paragraph()
    run = anchored.add_run("anchored")
    start = anchored._p.makeelement(
        qn("w:bookmarkStart"), {qn("w:id"): "55", qn("w:name"): "anchor_a"}
    )
    end = anchored._p.makeelement(qn("w:bookmarkEnd"), {qn("w:id"): "55"})
    run._r.addprevious(start)
    run._r.addnext(end)

    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "r0c0"
    table.cell(0, 1).text = "r0c1"
    table.cell(1, 0).text = "r1c0"
    nested = table.cell(1, 1).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "nested"

    doc.sections[0].header.paragraphs[0].text = "HEADER"
    doc.sections[0].footer.paragraphs[0].text = "FOOTER"
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def test_token_stream_order() -> None:
    doc = extract_docx(_sample_docx())
    paragraph = doc.part("document").paragraphs[1]
    assert [t.kind for t in paragraph.tokens] == [TEXT, TAB, TEXT, BREAK, TEXT]
    assert paragraph.tokens[0].text == "a"
    assert paragraph.tokens[1].text is None
    assert paragraph_plain_text(paragraph) == "a\tb\nc"


def test_body_paragraphs_come_before_table_in_flow_order() -> None:
    paragraphs = extract_docx(_sample_docx()).part("document").paragraphs
    texts = [paragraph_plain_text(p) for p in paragraphs]
    assert texts[:3] == ["plain text", "a\tb\nc", "anchored"]
    # cell(1,1) holds: its initial empty paragraph, the nested table's cell,
    # then the mandatory trailing paragraph python-docx appends after a table
    assert texts[3:] == ["r0c0", "r0c1", "r1c0", "", "nested", ""]


def test_table_cell_locators_and_nested_depth() -> None:
    paragraphs = extract_docx(_sample_docx()).part("document").paragraphs
    by_text = {paragraph_plain_text(p): p for p in paragraphs}
    assert by_text["r0c0"].in_table is True
    assert by_text["r0c0"].cell_locator == "tbl[0]/tr[0]/tc[0]"
    assert by_text["r1c0"].cell_locator == "tbl[0]/tr[1]/tc[0]"
    nested = by_text["nested"]
    assert nested.cell_locator == "tbl[0]/tr[1]/tc[1]/tbl[0]/tr[0]/tc[0]"
    assert nested.in_table is True
    assert by_text["plain text"].in_table is False
    assert by_text["plain text"].cell_locator is None


def test_style_ids_are_extracted() -> None:
    paragraphs = extract_docx(_sample_docx()).part("document").paragraphs
    assert paragraphs[0].style_id == "Heading1"
    # an unstyled paragraph has no w:pStyle: None, not a guessed "Normal"
    assert paragraphs[1].style_id is None


def test_bookmarks_covering_paragraph() -> None:
    paragraphs = extract_docx(_sample_docx()).part("document").paragraphs
    assert paragraphs[2].bookmark_names == ("anchor_a",)
    assert paragraphs[0].bookmark_names == ()


def test_header_footer_parts_enumerated_from_zip() -> None:
    doc = extract_docx(_sample_docx())
    assert [p.name for p in doc.parts] == ["document", "header1", "footer1"]
    assert paragraph_plain_text(doc.part("header1").paragraphs[0]) == "HEADER"
    assert paragraph_plain_text(doc.part("footer1").paragraphs[0]) == "FOOTER"


def test_document_plain_text_joins_paragraphs() -> None:
    doc = extract_docx(_sample_docx())
    text = document_plain_text(doc)
    assert "plain text\na\tb\nc\nanchored" in text
    assert "HEADER" not in text  # headers are separate parts


def test_document_plain_text_unknown_part_raises() -> None:
    doc = extract_docx(_sample_docx())
    with pytest.raises(ValueError, match="unknown part"):
        document_plain_text(doc, "header9")


def test_doctype_rejected_by_extractor() -> None:
    document = (
        '<!DOCTYPE w:document><w:document xmlns:w='
        '"http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p/></w:body></w:document>"
    )
    with pytest.raises(ValueError, match="DOCTYPE"):
        extract_docx(build_docx("", document=document))


def test_non_zip_rejected_by_extractor() -> None:
    with pytest.raises(ValueError, match="ZIP"):
        extract_docx(b"garbage")


def test_missing_document_part_rejected() -> None:
    from minidocx import CONTENT_TYPES, ROOT_RELS, build_zip

    docx = build_zip({"[Content_Types].xml": CONTENT_TYPES, "_rels/.rels": ROOT_RELS})
    with pytest.raises(ValueError, match="document"):
        extract_docx(docx)
