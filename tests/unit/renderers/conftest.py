"""Self-contained fixtures and helpers for the DOCX renderer unit tests.

Nothing here imports fixtures from other test packages. Templates are built
in memory with python-docx; the TemplateContract is assembled by hand with
anchors=None (anchor validation is the extractor's job — rendering does not
depend on anchors). The read-back extractor mirrors gate B's token mapping
(w:t text, w:tab -> "\\t", w:br -> "\\n") and lives here because tests must
not depend on production extraction code.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from docx import Document
from lxml import etree

from omas.domain.ids import new_artifact_id, new_task_id, new_template_version_id
from omas.domain.ir import DocxRenderIR, RenderSlot
from omas.domain.spans import SourceSpanRef
from omas.domain.template import (
    PlanSectionTemplate,
    SlotSpec,
    TemplateContract,
    TemplatePackageHashes,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
DOCUMENT_XML = "word/document.xml"

DEFAULT_SLOT_IDS = ("sales_summary", "risks", "next_plan")
STATIC_TITLE = "项目周报"
STATIC_INTRO = "本期内容由 OMAS 从已验证来源装配，未做改写。"
STATIC_TABLE = (("区域", "说明"), ("销售", "数字见上"))


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- construction


def build_template_docx(
    slot_ids: Sequence[str] = DEFAULT_SLOT_IDS,
    *,
    extra_placeholders: Sequence[str] = (),
) -> bytes:
    """Minimal template: static title + intro, one paragraph per placeholder,
    then a static 2x2 table."""
    doc = Document()
    doc.add_paragraph(STATIC_TITLE)
    doc.add_paragraph(STATIC_INTRO)
    for slot_id in slot_ids:
        doc.add_paragraph("{{ " + slot_id + " }}")
    for extra in extra_placeholders:
        doc.add_paragraph(extra)
    table = doc.add_table(rows=len(STATIC_TABLE), cols=2)
    for row, (left, right) in enumerate(STATIC_TABLE):
        table.cell(row, 0).text = left
        table.cell(row, 1).text = right
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_contract(
    docx_bytes: bytes,
    slot_ids: Sequence[str] = DEFAULT_SLOT_IDS,
    *,
    allow_user_omit: Sequence[str] = ("next_plan",),
) -> TemplateContract:
    """Hand-assembled contract over the given template bytes (real sha256)."""
    slots = tuple(
        SlotSpec(
            slot_id=slot_id,
            placeholder="{{ " + slot_id + " }}",
            kind="text_block",
            required=slot_id not in allow_user_omit,
            allow_user_omit=slot_id in allow_user_omit,
            semantic_requirement="test semantic requirement for " + slot_id,
            style_key="body",
            anchor=None,
        )
        for slot_id in slot_ids
    )
    hashes = TemplatePackageHashes(
        docx_sha256=hashlib.sha256(docx_bytes).hexdigest(),
        contract_sha256=_digest("contract"),
        styles_sha256=_digest("styles"),
        static_map_sha256=_digest("static-map"),
    )
    return TemplateContract(
        template_id="weekly-report-test",
        version=1,
        extractor_version="test-extractor-1",
        hashes=hashes,
        sections=(
            PlanSectionTemplate(section_id="s1", title="main", slot_ids=tuple(slot_ids)),
        ),
        slots=slots,
    )


def build_spans(*texts: str) -> tuple[SourceSpanRef, ...]:
    """One distinct (deterministic) span ref per text."""
    return tuple(
        SourceSpanRef(
            artifact_id=new_artifact_id(),
            canonical_sha256=_digest("canonical:" + text),
            start=0,
            end=len(text),
            span_sha256=_digest(text),
        )
        for text in texts
    )


def build_render_ir(slots: Sequence[RenderSlot]) -> DocxRenderIR:
    return DocxRenderIR(
        schema_version=1,
        task_id=new_task_id(),
        epoch=1,
        template_version_id=new_template_version_id(),
        template_docx_artifact_id=new_artifact_id(),
        plan_artifact_id=new_artifact_id(),
        binding_artifact_id=new_artifact_id(),
        slots=tuple(slots),
    )


class RecordingResolver:
    """Span resolver test double: dict-backed, records every call."""

    def __init__(self) -> None:
        self.texts: dict[tuple[str, int, int], str] = {}
        self.calls: list[SourceSpanRef] = []

    def given(self, spans: Sequence[SourceSpanRef], texts: Sequence[str]) -> None:
        for ref, text in zip(spans, texts, strict=True):
            self.texts[(ref.artifact_id, ref.start, ref.end)] = text

    def __call__(self, ref: SourceSpanRef) -> str:
        self.calls.append(ref)
        key = (ref.artifact_id, ref.start, ref.end)
        if key not in self.texts:
            raise AssertionError(f"resolver received an unexpected span: {key}")
        return self.texts[key]


# ------------------------------------------------------------------ read-back


def _document_root(docx_bytes: bytes) -> Any:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return etree.fromstring(zf.read(DOCUMENT_XML))


def paragraph_logical_text(paragraph: Any) -> str:
    """Logical text of one paragraph: w:t content, w:tab -> \\t, w:br -> \\n."""
    parts: list[str] = []
    for element in paragraph.iter():
        if element.tag == f"{W}t":
            parts.append(element.text or "")
        elif element.tag == f"{W}tab":
            parts.append("\t")
        elif element.tag == f"{W}br":
            parts.append("\n")
    return "".join(parts)


def body_paragraph_texts(docx_bytes: bytes) -> list[str]:
    """Top-level body paragraphs (table cells excluded), in document order."""
    body = _document_root(docx_bytes).find(f"{W}body")
    return [paragraph_logical_text(p) for p in body.findall(f"{W}p")]


def table_cell_texts(docx_bytes: bytes) -> list[list[list[str]]]:
    """Static tables as table -> row -> cell paragraph texts (cells joined by \\n)."""
    body = _document_root(docx_bytes).find(f"{W}body")
    tables: list[list[list[str]]] = []
    for tbl in body.findall(f"{W}tbl"):
        rows: list[list[str]] = []
        for tr in tbl.findall(f"{W}tr"):
            rows.append(
                [
                    "\n".join(paragraph_logical_text(p) for p in tc.findall(f"{W}p"))
                    for tc in tr.findall(f"{W}tc")
                ]
            )
        tables.append(rows)
    return tables


def document_xml_text(docx_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return zf.read(DOCUMENT_XML).decode("utf-8")


# ------------------------------------------------------------------- fixtures


@pytest.fixture
def make_template_docx() -> Callable[..., bytes]:
    return build_template_docx


@pytest.fixture
def make_contract() -> Callable[..., TemplateContract]:
    return build_contract


@pytest.fixture
def make_spans() -> Callable[..., tuple[SourceSpanRef, ...]]:
    return build_spans


@pytest.fixture
def make_ir() -> Callable[[Sequence[RenderSlot]], DocxRenderIR]:
    return build_render_ir


@pytest.fixture
def template_docx() -> bytes:
    return build_template_docx()


@pytest.fixture
def contract(template_docx: bytes) -> TemplateContract:
    return build_contract(template_docx)


@pytest.fixture
def resolver() -> RecordingResolver:
    return RecordingResolver()


@pytest.fixture
def paragraph_texts() -> Callable[[bytes], list[str]]:
    return body_paragraph_texts


@pytest.fixture
def table_texts() -> Callable[[bytes], list[list[list[str]]]]:
    return table_cell_texts


@pytest.fixture
def document_xml() -> Callable[[bytes], str]:
    return document_xml_text
