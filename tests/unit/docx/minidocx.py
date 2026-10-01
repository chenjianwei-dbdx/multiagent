"""Hand-rolled minimal DOCX packages for extraction/safety tests.

python-docx covers the "realistic document" cases; these helpers give exact
control over the raw OOXML (fields, revisions, broken styles, hostile ZIPs).
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" ContentType='
    b'"application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="xml" ContentType="application/xml"/>'
    b'<Override PartName="/word/document.xml" ContentType='
    b'"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    b"</Types>"
)

ROOT_RELS = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Id="rId1" Type='
    b'"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
    b' Target="word/document.xml"/>'
    b"</Relationships>"
)

DOC_RELS = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
)

#: minimal styles.xml: docDefaults with theme-only fonts + sz, plus a
#: Normal default style and a derived style for chain tests.
STYLES_TEMPLATE = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W}">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:asciiTheme="minorHAnsi" w:eastAsiaTheme="minorEastAsia"/>
        <w:sz w:val="22"/>
      </w:rPr>
    </w:rPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Body">
    <w:name w:val="Body"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr><w:rFonts w:ascii="Cambria"/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Centered">
    <w:name w:val="Centered"/>
    <w:basedOn w:val="Body"/>
    <w:pPr><w:jc w:val="center"/></w:pPr>
  </w:style>
</w:styles>
""".encode()

NUMBERING_TEMPLATE = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="{W}">
  <w:abstractNum w:abstractNumId="7">
    <w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="·"/></w:lvl>
    <w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/><w:lvlText w:val="·"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="3"><w:abstractNumId w:val="7"/></w:num>
</w:numbering>
""".encode()


def document_xml(body: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    )


def build_zip(entries: dict[str, bytes], *, duplicate: list[str] | None = None) -> bytes:
    """In-memory ZIP; *duplicate* entries are written twice verbatim."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name in duplicate or []:
            zf.writestr(name, entries[name])
    return buffer.getvalue()


def build_docx(
    body: str,
    *,
    document: str | None = None,
    styles: bytes | None = STYLES_TEMPLATE,
    numbering: bytes | None = None,
    extra: dict[str, bytes] | None = None,
    content_types: bytes | None = None,
) -> bytes:
    """Minimal but structurally valid DOCX with customizable parts."""
    entries: dict[str, bytes] = {
        "[Content_Types].xml": content_types or CONTENT_TYPES,
        "_rels/.rels": ROOT_RELS,
        "word/_rels/document.xml.rels": DOC_RELS,
        "word/document.xml": (document if document is not None else document_xml(body)).encode(),
    }
    if styles is not None:
        entries["word/styles.xml"] = styles
    if numbering is not None:
        entries["word/numbering.xml"] = numbering
    if extra:
        entries.update(extra)
    return build_zip(entries)


def para(text: str, style: str | None = None, extra_ppr: str = "", run_rpr: str = "") -> str:
    ppr = ""
    style_part = f'<w:pStyle w:val="{style}"/>' if style else ""
    if style_part or extra_ppr:
        ppr = f"<w:pPr>{style_part}{extra_ppr}</w:pPr>"
    return f"<w:p>{ppr}<w:r>{run_rpr}<w:t>{text}</w:t></w:r></w:p>"


def rewrite_document(docx_bytes: bytes, transform: Callable[[str], str]) -> bytes:
    """Rewrite word/document.xml through *transform* (str -> str)."""
    src = zipfile.ZipFile(io.BytesIO(docx_bytes))
    try:
        document = src.read("word/document.xml").decode("utf-8")
        entries = {info.filename: src.read(info.filename) for info in src.infolist()}
    finally:
        src.close()
    entries["word/document.xml"] = transform(document).encode("utf-8")
    return build_zip(entries)
