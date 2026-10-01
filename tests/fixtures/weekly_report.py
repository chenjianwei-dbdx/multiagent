"""Contract weekly-report template fixture (P1 shared fixture, v1.1 §3.1).

Builds, fully deterministically (python-docx has no clock/randomness in its
default template), the DOCX plus its semantic sidecar, styles spec, static
map, example slot texts and the matching material Markdown. The construction
rules mirror the supported template subset:

- placeholders occupy their own paragraph, inside a single run, exactly once;
- each placeholder paragraph is covered by a bookmark named after its slot;
- every static area (title, bullets, headings, table cells, header) is listed
  in the static map;
- the ``Normal`` style carries explicit literal fonts (Calibri / SimSun /
  21 half-points) so no theme-indirect reference is involved.
"""

from __future__ import annotations

import functools
import io
from dataclasses import dataclass
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from omas.docx.extract import extract_docx, paragraph_plain_text

TEMPLATE_ID = "weekly-report"

#: bookmark ids for the three slots (stable across versions of this fixture)
_BOOKMARK_IDS = {"sales_summary": "100", "risks": "101", "next_plan": "102"}

_SLOT_PLACEHOLDERS = {
    "sales_summary": "{{ sales_summary }}",
    "risks": "{{ risks }}",
    "next_plan": "{{ next_plan }}",
}


@dataclass(frozen=True, slots=True)
class WeeklyReportFixture:
    docx_bytes: bytes
    sidecar: dict[str, Any]
    styles_spec: dict[str, Any]
    static_map: dict[str, Any]
    slot_texts: dict[str, str]
    material_markdown: str


def build_weekly_report_template() -> WeeklyReportFixture:
    """Construct the weekly-report template package (deterministic)."""
    docx_bytes = _build_docx_bytes()
    static_map = _build_static_map(docx_bytes)
    return WeeklyReportFixture(
        docx_bytes=docx_bytes,
        sidecar=_build_sidecar(),
        styles_spec=_build_styles_spec(),
        static_map=static_map,
        slot_texts=dict(_SLOT_TEXTS),
        material_markdown=_MATERIAL_MARKDOWN,
    )


@functools.lru_cache(maxsize=1)
def _build_docx_bytes() -> bytes:
    doc = Document()

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)  # 21 half-points
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "SimSun")

    doc.add_paragraph("项目周报", style="Title")
    doc.add_paragraph("报告周期：W40", style="List Bullet")
    doc.add_paragraph("编制：OMAS", style="List Bullet")

    headings = ("一、本周销售情况", "二、风险与依赖", "三、下周计划")
    for heading, slot_id in zip(headings, _BOOKMARK_IDS, strict=True):
        doc.add_paragraph(heading, style="Heading 2")
        paragraph = doc.add_paragraph()
        run = paragraph.add_run(_SLOT_PLACEHOLDERS[slot_id])
        bookmark_start = paragraph._p.makeelement(
            qn("w:bookmarkStart"),
            {qn("w:id"): _BOOKMARK_IDS[slot_id], qn("w:name"): slot_id},
        )
        bookmark_end = paragraph._p.makeelement(
            qn("w:bookmarkEnd"), {qn("w:id"): _BOOKMARK_IDS[slot_id]}
        )
        run._r.addprevious(bookmark_start)
        run._r.addnext(bookmark_end)

    table = doc.add_table(rows=2, cols=2)
    table.style = "Table Grid"
    table.cell(0, 0).text = "事项"
    table.cell(0, 1).text = "说明"
    table.cell(1, 0).text = "密级"
    table.cell(1, 1).text = "内部"

    doc.sections[0].header.paragraphs[0].text = "内部资料 · 请勿外传"

    buffer = io.BytesIO()
    for paragraph in doc.paragraphs:
        if paragraph.text.strip().startswith("{{"):
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            paragraph.paragraph_format.left_indent = 0
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
    doc.save(buffer)
    return _repack_deterministic(buffer.getvalue())


def _repack_deterministic(docx_bytes: bytes) -> bytes:
    """Rewrite the zip with fixed entry order and timestamps.

    python-docx stamps zip entries with the current time, which would make
    the "deterministic" fixture differ across processes (and break hash
    comparisons against a registered template version).
    """
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as source:
        entries = sorted(source.infolist(), key=lambda info: info.filename)
        payload = {info.filename: source.read(info.filename) for info in entries}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in payload.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            target.writestr(info, data)
    return buffer.getvalue()


def _build_sidecar() -> dict[str, Any]:
    slots: list[dict[str, Any]] = [
        {
            "slot_id": "sales_summary",
            "placeholder": "{{ sales_summary }}",
            "kind": "text_block",
            "required": True,
            "allow_user_omit": False,
            "semantic_requirement": "本周销售情况",
            "style_key": "body",
        },
        {
            "slot_id": "risks",
            "placeholder": "{{ risks }}",
            "kind": "text_block",
            "required": True,
            "allow_user_omit": True,
            "semantic_requirement": "风险与依赖",
            "style_key": "body",
        },
        {
            "slot_id": "next_plan",
            "placeholder": "{{ next_plan }}",
            "kind": "text_block",
            "required": True,
            "allow_user_omit": False,
            "semantic_requirement": "下周计划",
            "style_key": "body",
        },
    ]
    sections = [
        {"section_id": "sales", "title": "一、本周销售情况", "slot_ids": ["sales_summary"]},
        {"section_id": "risks", "title": "二、风险与依赖", "slot_ids": ["risks"]},
        {"section_id": "next_plan", "title": "三、下周计划", "slot_ids": ["next_plan"]},
    ]
    return {
        "schema_version": "1.1",
        "template_id": TEMPLATE_ID,
        "slots": slots,
        "sections": sections,
    }


def _build_styles_spec() -> dict[str, Any]:
    return {
        "body": {
            "paragraph_style": "Normal",
            "font_latin": "Calibri",
            "font_east_asia": "SimSun",
            "font_size_half_points": 21,
            "alignment": "left",
            "indent_left_twips": 0,
            "margin_top_twips": 1440,
            "margin_bottom_twips": 1440,
        },
        "heading": {
            "paragraph_style": "Heading2",
            "bold": True,
            "font_size_half_points": 26,
        },
    }


def _build_static_map(docx_bytes: bytes) -> dict[str, Any]:
    """Locate the static areas through our own extractor (no magic numbers)."""
    extracted = extract_docx(docx_bytes)
    document = extracted.part("document")
    if document is None:  # pragma: no cover - the fixture always has one
        raise AssertionError("fixture docx has no document part")
    texts = [paragraph_plain_text(p) for p in document.paragraphs]

    def index_of(text: str) -> int:
        return texts.index(text)

    def range_of(*texts: str) -> list[int]:
        indexes = [index_of(t) for t in texts]
        return [min(indexes), max(indexes)]

    header = extracted.part("header1")
    if header is None:  # pragma: no cover - python-docx always emits header1
        raise AssertionError("fixture docx has no header1 part")

    regions = [
        {
            "region_id": "title",
            "part": "document",
            "locator": "document:para[0]",
            "paragraph_range": [index_of("项目周报")] * 2,
        },
        {
            "region_id": "meta-bullets",
            "part": "document",
            "locator": "document:bullets",
            "paragraph_range": range_of("报告周期：W40", "编制：OMAS"),
        },
        {
            "region_id": "heading-sales",
            "part": "document",
            "locator": "document:heading-sales",
            "paragraph_range": [index_of("一、本周销售情况")] * 2,
        },
        {
            "region_id": "heading-risks",
            "part": "document",
            "locator": "document:heading-risks",
            "paragraph_range": [index_of("二、风险与依赖")] * 2,
        },
        {
            "region_id": "heading-plan",
            "part": "document",
            "locator": "document:heading-plan",
            "paragraph_range": [index_of("三、下周计划")] * 2,
        },
        {
            "region_id": "classification-table",
            "part": "document",
            "locator": "document:tbl[0]",
            "table_ref": "tbl[0]",
        },
        {
            "region_id": "header-banner",
            "part": "header1",
            "locator": "header1:para[0]",
            "paragraph_range": [0, 0],
        },
    ]
    return {"schema_version": "1", "template_id": TEMPLATE_ID, "regions": regions}


_SLOT_TEXTS: dict[str, str] = {
    "sales_summary": "本周实现销售额 1,280 万元，环比增长 12.5%，新增签约客户 3 家。",
    "risks": "上游数据接口尚未联调完成，存在 2 天延期风险；已在跟踪列表中登记。",
    "next_plan": "下周完成接口联调与回归测试，计划于周五前交付候选版本。",
}

_MATERIAL_MARKDOWN = """# 项目周报材料

## 一、本周销售情况

本周实现销售额 1,280 万元，环比增长 12.5%。华东区贡献 640 万元，占总额一半。
新增签约客户 3 家，其中 2 家为年度框架协议。

## 二、风险与依赖

上游数据接口尚未联调完成，存在 2 天延期风险。依赖平台组提供测试环境，已登记跟踪。

## 三、下周计划

下周完成接口联调与回归测试，计划于周五前交付候选版本，并同步启动验收材料准备。
"""
