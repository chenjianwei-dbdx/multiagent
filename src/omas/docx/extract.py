"""Standalone OOXML text/structure extraction — the shared base of gate B and
the format gate (v1.1 §7).

This module never uses python-docx high-level APIs for extraction: probe 3/4
showed ``w:br``/``w:tab`` are lost by naive text joins and style chains are
invisible to ``run.font``. Text is emitted as an ordered token stream per
paragraph, tables are expanded in document-flow order and every paragraph
records its style id, table position and covering bookmarks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum

from omas.domain.template import UnsupportedFinding

from ._xml import (
    W_NS,
    XmlElement,
    canonical_bytes,
    find_child,
    find_children,
    get_attr,
    iter_tag,
    local_name,
    namespace_of,
    parse_xml,
    w,
)
from ._zipio import open_zip, read_capped, text_part_names

__all__ = [
    "BREAK",
    "TAB",
    "TEXT",
    "DocumentDetail",
    "ExtractedDocument",
    "ExtractedParagraph",
    "ExtractedPart",
    "ParagraphDetail",
    "TextToken",
    "TextTokenKind",
    "document_plain_text",
    "extract_details",
    "extract_docx",
    "find_unsupported_text_containers",
    "paragraph_plain_text",
    "part_display_name",
    "structure_signature",
]


class TextTokenKind(StrEnum):
    TEXT = "text"
    TAB = "tab"
    BREAK = "break"


TEXT = TextTokenKind.TEXT
TAB = TextTokenKind.TAB
BREAK = TextTokenKind.BREAK


@dataclass(frozen=True, slots=True)
class TextToken:
    kind: TextTokenKind
    text: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractedParagraph:
    tokens: tuple[TextToken, ...]
    style_id: str | None
    in_table: bool
    cell_locator: str | None
    bookmark_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExtractedPart:
    name: str
    paragraphs: tuple[ExtractedParagraph, ...]


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    parts: tuple[ExtractedPart, ...]

    def part(self, name: str) -> ExtractedPart | None:
        return next((p for p in self.parts if p.name == name), None)


@dataclass(frozen=True, slots=True)
class BookmarkRef:
    """A ``w:bookmarkStart`` covering a paragraph, with its pairing state."""

    id: str
    name: str
    has_matching_end: bool


@dataclass(frozen=True, slots=True)
class ParagraphDetail:
    """Extraction record with the underlying element for gate-level checks."""

    part_name: str
    index: int
    paragraph: ExtractedParagraph
    run_texts: tuple[str, ...]
    bookmarks: tuple[BookmarkRef, ...]
    signature: str
    element: XmlElement = field(repr=False, default=None)


@dataclass(frozen=True, slots=True)
class PartDetail:
    name: str
    paragraphs: tuple[ParagraphDetail, ...]

    def to_extracted(self) -> ExtractedPart:
        return ExtractedPart(name=self.name, paragraphs=tuple(d.paragraph for d in self.paragraphs))


@dataclass(frozen=True, slots=True)
class DocumentDetail:
    parts: tuple[PartDetail, ...]

    def part(self, name: str) -> PartDetail | None:
        return next((p for p in self.parts if p.name == name), None)


# ---------------------------------------------------------------- extraction


def part_display_name(zip_name: str) -> str:
    """``word/header2.xml`` -> ``header2``; the document part is ``document``."""
    if zip_name == "word/document.xml":
        return "document"
    stem = zip_name.removeprefix("word/").removesuffix(".xml")
    return stem


def structure_signature(element: XmlElement) -> str:
    """sha256 over the canonical serialization of a paragraph's XML subtree."""
    return hashlib.sha256(canonical_bytes(element)).hexdigest()


def extract_docx(docx_bytes: bytes) -> ExtractedDocument:
    """Public extraction API: token stream per paragraph, in document flow.

    Raises :class:`ValueError` for unreadable archives or unparseable XML —
    run :func:`omas.docx.zipcheck.check_zip_safety` first to surface those as
    findings.
    """
    parts = extract_details(docx_bytes).parts
    return ExtractedDocument(parts=tuple(p.to_extracted() for p in parts))


def extract_details(docx_bytes: bytes) -> DocumentDetail:
    """Extraction with underlying elements (used by gates and the resolver)."""
    zf = open_zip(docx_bytes)
    with zf:
        parts: list[PartDetail] = []
        for zip_name in text_part_names(zf):
            data = read_capped(zf, zip_name)
            root = parse_xml(data, source=zip_name)
            parts.append(_extract_part(root, part_display_name(zip_name)))
        if not any(p.name == "document" for p in parts):
            raise ValueError("archive has no word/document.xml part")
        return DocumentDetail(parts=tuple(parts))


def _extract_part(root: XmlElement, name: str) -> PartDetail:
    container = root if local_name(root) in ("hdr", "ftr") else find_child(root, w("body"))
    if container is None:
        container = root
    end_ids = {get_attr(el, w("id")) or "" for el in iter_tag(root, w("bookmarkEnd"))}
    records: list[_ParagraphRecord] = []
    _walk_container(container, records, in_table=False, cell_locator=None)
    details = []
    for index, record in enumerate(records):
        paragraph_el = record.element
        bookmarks = tuple(
            BookmarkRef(
                id=get_attr(bs, w("id")) or "",
                name=get_attr(bs, w("name")) or "",
                has_matching_end=(get_attr(bs, w("id")) or "") in end_ids,
            )
            for bs in iter_tag(paragraph_el, w("bookmarkStart"))
        )
        tokens = _tokens(paragraph_el)
        details.append(
            ParagraphDetail(
                part_name=name,
                index=index,
                paragraph=ExtractedParagraph(
                    tokens=tokens,
                    style_id=_style_id(paragraph_el),
                    in_table=record.in_table,
                    cell_locator=record.cell_locator,
                    bookmark_names=tuple(b.name for b in bookmarks),
                ),
                run_texts=_run_texts(paragraph_el),
                bookmarks=bookmarks,
                signature=structure_signature(paragraph_el),
                element=paragraph_el,
            )
        )
    return PartDetail(name=name, paragraphs=tuple(details))


@dataclass(slots=True)
class _ParagraphRecord:
    element: XmlElement
    in_table: bool
    cell_locator: str | None


def _walk_container(
    container: XmlElement,
    records: list[_ParagraphRecord],
    *,
    in_table: bool,
    cell_locator: str | None,
) -> None:
    """Append paragraphs in document-flow order: paragraphs and tables, then
    each table row-by-row, cell-by-cell (recursing into nested tables)."""
    table_index = 0
    for child in container:
        if namespace_of(child) != W_NS:
            continue
        tag = local_name(child)
        if tag == "p":
            records.append(_ParagraphRecord(child, in_table, cell_locator))
        elif tag == "tbl":
            if cell_locator is None:
                table_loc = f"tbl[{table_index}]"
            else:
                table_loc = f"{cell_locator}/tbl[{table_index}]"
            for row_index, tr in enumerate(find_children(child, w("tr"))):
                for cell_index, tc in enumerate(find_children(tr, w("tc"))):
                    locator = f"{table_loc}/tr[{row_index}]/tc[{cell_index}]"
                    _walk_container(tc, records, in_table=True, cell_locator=locator)
            table_index += 1
        # other block content (sectPr, sdt, altChunk, ...) carries no directly
        # supported text; unsupported containers are flagged separately.


def _tokens(paragraph_el: XmlElement) -> tuple[TextToken, ...]:
    tokens: list[TextToken] = []
    for element in paragraph_el.iter():
        if namespace_of(element) != W_NS:
            continue
        tag = local_name(element)
        if tag == "t":
            tokens.append(TextToken(TEXT, element.text or ""))
        elif tag == "tab":
            tokens.append(TextToken(TAB))
        elif tag == "br":
            tokens.append(TextToken(BREAK))
    return tuple(tokens)


def _run_texts(paragraph_el: XmlElement) -> tuple[str, ...]:
    """Logical text of each ``w:r`` in document order (probe 3 reverse map)."""
    texts: list[str] = []
    for run in iter_tag(paragraph_el, w("r")):
        parts: list[str] = []
        for child in run:
            if namespace_of(child) != W_NS:
                continue
            tag = local_name(child)
            if tag == "t":
                parts.append(child.text or "")
            elif tag == "tab":
                parts.append("\t")
            elif tag == "br":
                parts.append("\n")
        texts.append("".join(parts))
    return tuple(texts)


def _style_id(paragraph_el: XmlElement) -> str | None:
    ppr = find_child(paragraph_el, w("pPr"))
    if ppr is None:
        return None
    pstyle = find_child(ppr, w("pStyle"))
    if pstyle is None:
        return None
    return get_attr(pstyle, w("val"))


def paragraph_plain_text(p: ExtractedParagraph) -> str:
    """w:t joined in order; ``w:tab`` -> ``\\t``; ``w:br`` -> ``\\n``."""
    chunks: list[str] = []
    for token in p.tokens:
        if token.kind is TEXT:
            chunks.append(token.text or "")
        elif token.kind is TAB:
            chunks.append("\t")
        else:
            chunks.append("\n")
    return "".join(chunks)


def document_plain_text(doc: ExtractedDocument, part: str = "document") -> str:
    """Paragraph plain texts joined with ``\\n`` (one paragraph boundary)."""
    extracted = doc.part(part)
    if extracted is None:
        raise ValueError(f"unknown part {part!r}; have {[p.name for p in doc.parts]}")
    return "\n".join(paragraph_plain_text(p) for p in extracted.paragraphs)


# ------------------------------------------------- unsupported text carriers

#: (element local name, requires w: namespace, finding code)
_UNSUPPORTED_ELEMENTS: tuple[tuple[str, bool, str], ...] = (
    ("fldSimple", True, "UNSUPPORTED_FIELD"),
    ("instrText", True, "UNSUPPORTED_FIELD"),
    ("fldChar", True, "UNSUPPORTED_FIELD"),
    ("ins", True, "UNSUPPORTED_REVISION"),
    ("del", True, "UNSUPPORTED_REVISION"),
    ("commentReference", True, "UNSUPPORTED_COMMENT"),
    ("commentRangeStart", True, "UNSUPPORTED_COMMENT"),
    ("commentRangeEnd", True, "UNSUPPORTED_COMMENT"),
    ("txbxContent", True, "UNSUPPORTED_TEXTBOX"),
    ("object", True, "UNSUPPORTED_OLE_OBJECT"),
    ("OLEObject", False, "UNSUPPORTED_OLE_OBJECT"),
    ("sdt", True, "UNSUPPORTED_STRUCTURED_TAG"),
    ("altChunk", True, "UNSUPPORTED_ALT_CHUNK"),
)

_UNSUPPORTED_PARTS = (
    ("word/footnotes.xml", "UNSUPPORTED_FOOTNOTES_PART"),
    ("word/endnotes.xml", "UNSUPPORTED_ENDNOTES_PART"),
    ("word/comments.xml", "UNSUPPORTED_COMMENT"),
)


def find_unsupported_text_containers(docx_bytes: bytes) -> tuple[UnsupportedFinding, ...]:
    """Detect text carriers outside the supported subset (v1.1 §3.2).

    Checks every document/header/footer part for fields, revisions, comments,
    text boxes, OLE objects, structured tags and altChunks, and the archive
    for footnotes/endnotes/comments parts. Never raises; unreadable archives
    are reported as a finding.
    """
    findings: list[UnsupportedFinding] = []
    try:
        zf = open_zip(docx_bytes)
    except ValueError as exc:
        return (UnsupportedFinding(code="ZIP_INVALID", locator="package", detail=str(exc)),)
    with zf:
        names = set(zf.namelist())
        for part_name, code in _UNSUPPORTED_PARTS:
            if part_name in names:
                findings.append(
                    UnsupportedFinding(
                        code=code, locator=part_name, detail="unsupported text-bearing part"
                    )
                )
        for zip_name in text_part_names(zf):
            try:
                root = parse_xml(read_capped(zf, zip_name), source=zip_name)
            except ValueError as exc:
                findings.append(
                    UnsupportedFinding(code="XML_UNPARSEABLE", locator=zip_name, detail=str(exc))
                )
                continue
            findings.extend(_scan_part(root, zip_name))
    return tuple(findings)


def _scan_part(root: XmlElement, part_name: str) -> list[UnsupportedFinding]:
    hits: dict[str, list[XmlElement]] = {}
    for element in root.iter():
        name = local_name(element)
        ns_is_w = namespace_of(element) == W_NS
        for candidate, requires_w, code in _UNSUPPORTED_ELEMENTS:
            if name == candidate and (ns_is_w or not requires_w):
                hits.setdefault(code, []).append(element)
                break
    findings: list[UnsupportedFinding] = []
    for code, elements in sorted(hits.items()):
        findings.append(
            UnsupportedFinding(
                code=code,
                locator=f"{part_name}:{local_name(elements[0])}",
                detail=f"{len(elements)} occurrence(s) of unsupported element",
            )
        )
    return findings
