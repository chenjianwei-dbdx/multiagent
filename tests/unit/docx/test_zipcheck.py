"""ZIP/OOXML package safety checks (v1.1 §9)."""

from __future__ import annotations

import stat
import zipfile

from minidocx import CONTENT_TYPES, ROOT_RELS, build_docx, build_zip

from omas.docx.zipcheck import MAX_DOCX_BYTES, check_zip_safety


def codes(docx: bytes) -> set[str]:
    return {f.code for f in check_zip_safety(docx)}


def test_clean_minimal_docx_has_no_findings() -> None:
    assert check_zip_safety(build_docx("<w:p><w:r><w:t>ok</w:t></w:r></w:p>")) == ()


def test_python_docx_output_passes() -> None:
    import io

    from docx import Document

    doc = Document()
    doc.add_paragraph("hello", style="List Bullet")
    buffer = io.BytesIO()
    doc.save(buffer)
    assert check_zip_safety(buffer.getvalue()) == ()


def test_rejects_macro_part() -> None:
    docx = build_docx(
        "<w:p/>", extra={"word/vbaProject.bin": b"\x00\x01macro"}
    )
    assert "ZIP_MACRO_PART" in codes(docx)


def test_rejects_external_relationship() -> None:
    rels = (
        b'<?xml version="1.0"?><Relationships xmlns='
        b'"http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
        b'officeDocument/2006/relationships/hyperlink" Target="https://evil.example/x"'
        b' TargetMode="External"/></Relationships>'
    )
    docx = build_docx("<w:p/>", extra={"word/_rels/document.xml.rels": rels})
    assert "ZIP_EXTERNAL_RELATIONSHIP" in codes(docx)


def test_rejects_duplicate_entry_names() -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # zipfile warns on duplicates
        docx = build_zip(
            {
                "[Content_Types].xml": CONTENT_TYPES,
                "_rels/.rels": ROOT_RELS,
                "word/document.xml": b"<doc/>",
            },
            duplicate=["word/document.xml"],
        )
    assert "ZIP_DUPLICATE_ENTRY" in codes(docx)


def test_rejects_unsafe_entry_names() -> None:
    docx = build_zip(
        {
            "[Content_Types].xml": CONTENT_TYPES,
            "_rels/.rels": ROOT_RELS,
            "word/document.xml": b"<doc/>",
            "../escape.txt": b"x",
            "/absolute.txt": b"x",
            "a\\b.txt": b"x",
        }
    )
    findings = check_zip_safety(docx)
    unsafe = [f for f in findings if f.code == "ZIP_UNSAFE_ENTRY_NAME"]
    assert {f.locator for f in unsafe} == {"../escape.txt", "/absolute.txt", "a\\b.txt"}
    absolute = next(f for f in unsafe if f.locator == "/absolute.txt")
    assert absolute.detail == "absolute entry path"


def test_rejects_symlink_entry() -> None:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("word/document.xml", b"<doc/>")
        info = zipfile.ZipInfo("word/evil.txt")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, b"/etc/passwd")
    assert "ZIP_SYMLINK_ENTRY" in codes(buffer.getvalue())


def test_rejects_too_many_entries() -> None:
    entries = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": ROOT_RELS,
        "word/document.xml": b"<doc/>",
    }
    entries.update({f"word/p{i}.bin": b"x" for i in range(2_000)})
    assert "ZIP_TOO_MANY_ENTRIES" in codes(build_zip(entries))


def test_rejects_oversized_docx() -> None:
    assert "ZIP_OVERSIZED" in codes(b"\x00" * (MAX_DOCX_BYTES + 1))


def test_rejects_declared_expansion_over_limit() -> None:
    # 101 MiB of zeros compresses to ~100 KiB: the archive itself stays far
    # below the 20 MiB DOCX limit, only the declared total trips the rule.
    docx = build_zip(
        {
            "[Content_Types].xml": CONTENT_TYPES,
            "_rels/.rels": ROOT_RELS,
            "word/document.xml": b"<doc/>",
            "word/big.bin": b"0" * (101 * 1024 * 1024),
        }
    )
    assert len(docx) < MAX_DOCX_BYTES
    assert "ZIP_EXPANSION_OVER_LIMIT" in codes(docx)


def test_rejects_non_docx_structure() -> None:
    missing_types = build_zip({"word/document.xml": b"<doc/>"})
    assert "ZIP_MISSING_CONTENT_TYPES" in codes(missing_types)

    missing_document = build_zip({"[Content_Types].xml": CONTENT_TYPES})
    assert "ZIP_MISSING_DOCUMENT" in codes(missing_document)

    bogus_types = build_zip(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<doc/>",
        }
    )
    assert "ZIP_CONTENT_TYPES_INVALID" in codes(bogus_types)


def test_rejects_non_zip_input() -> None:
    assert "ZIP_INVALID" in codes(b"this is not a zip at all")


def test_rejects_doctype_in_xml_part() -> None:
    document = (
        '<!DOCTYPE w:document [<!ENTITY xxe "file:///etc/passwd">]>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>'
    )
    docx = build_docx("", document=document)
    assert "XML_DOCTYPE" in codes(docx)
