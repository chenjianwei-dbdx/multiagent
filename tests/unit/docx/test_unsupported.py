"""Unsupported text-container detection (v1.1 §3.2)."""

from __future__ import annotations

import io

from docx import Document
from minidocx import build_docx

from omas.docx import find_unsupported_text_containers

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def codes(docx: bytes) -> set[str]:
    return {f.code for f in find_unsupported_text_containers(docx)}


def test_clean_documents_have_no_findings() -> None:
    doc = Document()
    doc.add_paragraph("plain", style="List Bullet")
    doc.add_paragraph("{{ slot }}")
    buffer = io.BytesIO()
    doc.save(buffer)
    assert codes(buffer.getvalue()) == set()


def test_fld_simple_and_complex_fields() -> None:
    body = (
        '<w:p><w:fldSimple w:instr="PAGE"><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>'
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText>PAGE</w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    assert codes(build_docx(body)) == {"UNSUPPORTED_FIELD"}


def test_revisions_rejected() -> None:
    body = (
        '<w:p><w:ins w:id="1" w:author="a"><w:r><w:t>new</w:t></w:r></w:ins>'
        '<w:del w:id="2" w:author="a"><w:r><w:delText>old</w:delText></w:r></w:del></w:p>'
    )
    assert codes(build_docx(body)) == {"UNSUPPORTED_REVISION"}


def test_comment_reference_rejected() -> None:
    body = '<w:p><w:r><w:t>x</w:t></w:r><w:commentReference w:id="0"/></w:p>'
    assert codes(build_docx(body)) == {"UNSUPPORTED_COMMENT"}


def test_comment_part_rejected() -> None:
    comments = (
        f'<?xml version="1.0"?><w:comments xmlns:w="{W}">'
        f'<w:comment w:id="0" w:author="a"><w:p><w:r><w:t>c</w:t></w:r></w:p></w:comment>'
        f"</w:comments>"
    ).encode()
    assert codes(build_docx("<w:p/>", extra={"word/comments.xml": comments})) == {
        "UNSUPPORTED_COMMENT"
    }


def test_textbox_rejected() -> None:
    body = (
        "<w:p><w:r><w:drawing><mc:AlternateContent xmlns:mc="
        '"http://schemas.openxmlformats.org/markup-compatibility/2006">'
        "<w:txbxContent><w:p><w:r><w:t>in box</w:t></w:r></w:p></w:txbxContent>"
        "</mc:AlternateContent></w:drawing></w:r></w:p>"
    )
    assert codes(build_docx(body)) == {"UNSUPPORTED_TEXTBOX"}


def test_footnote_and_endnote_parts_rejected() -> None:
    notes = (
        f'<?xml version="1.0"?><w:footnotes xmlns:w="{W}"><w:footnote w:id="1">'
        f"<w:p><w:r><w:t>note</w:t></w:r></w:p></w:footnote></w:footnotes>"
    ).encode()
    endnotes = (
        f'<?xml version="1.0"?><w:endnotes xmlns:w="{W}"><w:endnote w:id="1">'
        f"<w:p><w:r><w:t>end</w:t></w:r></w:p></w:endnote></w:endnotes>"
    ).encode()
    assert codes(
        build_docx(
            "<w:p/>",
            extra={"word/footnotes.xml": notes, "word/endnotes.xml": endnotes},
        )
    ) == {"UNSUPPORTED_FOOTNOTES_PART", "UNSUPPORTED_ENDNOTES_PART"}


def test_ole_object_rejected() -> None:
    ole_ns = "urn:schemas-microsoft-com:office:office"
    body = (
        f'<w:p><w:r><w:object><o:OLEObject xmlns:o="{ole_ns}"/>'
        f"</w:object></w:r></w:p>"
    )
    assert codes(build_docx(body)) == {"UNSUPPORTED_OLE_OBJECT"}


def test_alt_chunk_rejected() -> None:
    body = '<w:altChunk r:id="rId9" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
    assert codes(build_docx(body)) == {"UNSUPPORTED_ALT_CHUNK"}


def test_structured_document_tag_rejected() -> None:
    body = '<w:sdt><w:sdtContent><w:p><w:r><w:t>in sdt</w:t></w:r></w:p></w:sdtContent></w:sdt>'
    assert codes(build_docx(body)) == {"UNSUPPORTED_STRUCTURED_TAG"}


def test_non_zip_input_reported_not_raised() -> None:
    assert "ZIP_INVALID" in codes(b"garbage")
