#!/usr/bin/env python3
"""Generate the built-in DOCX template assets shipped with OMAS.

Three generic Chinese report templates (工作报告 / 月报 / 调研报告) with a
Title / Heading 1 / Heading 2 structure. Each dynamic section is a
``{{ slot }}`` placeholder paragraph; the headings are static boilerplate.

The generated DOCX files plus ``builtin-templates.json`` (display metadata
and per-slot semantics) are the package data consumed at runtime by
:mod:`omas.templates.seed` — they live under ``src/omas/templates/builtin/``
so hatchling ships them in the wheel (``packages = ["src/omas"]``).

Structural rules honored so every template extracts with ZERO unsupported
findings and is activatable (see src/omas/templates/extractor.py):

- a placeholder paragraph holds exactly one run whose full text is
  ``{{ slot_id }}`` (single spaces), and nothing else;
- slot ids are ASCII identifiers (``[A-Za-z_][A-Za-z0-9_]*``);
- slot paragraphs use only Title/Normal styles — list styles carry
  numbering that the format gate rejects on slot paragraphs;
- no tables, headers/footers, fields, page breaks or comments: those are
  unsupported text containers or would desync the auto-generated static
  map (scaffold counts body-level paragraphs only).

Usage::

    templates/build_builtin_templates.py

Each generated template is pre-checked through the real
``scaffold_and_register`` against a throwaway OMAS_HOME (ledger + artifact
pool in a temp dir) — anything not activatable fails the build before the
asset files are touched. Registration into a real library happens through
``omas template seed`` or ``omas web`` startup, never from this script.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

HERE = Path(__file__).parent
#: package data directory consumed by omas.templates.seed at runtime
BUILTIN_DIR = HERE.parent / "src" / "omas" / "templates" / "builtin"

#: paragraph kinds; "title" and "slot" paragraphs are dynamic, the rest static
BlockKind = Literal["title", "h1", "h2", "slot"]

_STYLE_FOR_KIND: dict[BlockKind, str | None] = {
    "title": "Title",
    "h1": "Heading 1",
    "h2": "Heading 2",
    "slot": None,  # plain Normal paragraph — matches the scaffold's styles spec
}


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    """One built-in template: structure, display metadata and slot semantics."""

    template_id: str
    display_name: str
    description: str
    blocks: tuple[tuple[BlockKind, str], ...]
    semantics: dict[str, str] = field(default_factory=dict)

    @property
    def docx_name(self) -> str:
        return f"{self.template_id}.docx"


TEMPLATES: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        template_id="work-report",
        display_name="工作报告",
        description="通用工作报告模板：工作总结、重点成绩、存在问题与下一步安排。",
        blocks=(
            ("title", "{{ report_title }}"),
            ("h1", "一、工作总结"),
            ("slot", "{{ work_summary }}"),
            ("h1", "二、重点工作完成情况"),
            ("h2", "（一）主要成绩"),
            ("slot", "{{ key_results }}"),
            ("h2", "（二）存在的问题"),
            ("slot", "{{ open_issues }}"),
            ("h1", "三、下一步工作安排"),
            ("slot", "{{ next_steps }}"),
        ),
        semantics={
            "report_title": "工作报告的完整标题，通常包含汇报主体与时间段，"
            "例如“2026年第三季度销售部工作报告”",
            "work_summary": "报告期内整体工作情况的总结性陈述，概括主要目标与完成情况",
            "key_results": "报告期内已完成的主要任务、项目及其成果",
            "open_issues": "工作推进中遇到的主要问题、困难或未达预期的事项",
            "next_steps": "下一阶段的工作计划、目标与主要措施",
        },
    ),
    TemplateSpec(
        template_id="monthly-report",
        display_name="月报",
        description="通用月报模板：本月概况、核心指标与进展、问题风险、下月计划。",
        blocks=(
            ("title", "{{ report_title }}"),
            ("h1", "一、本月概况"),
            ("slot", "{{ monthly_overview }}"),
            ("h1", "二、主要指标与进展"),
            ("h2", "（一）核心指标"),
            ("slot", "{{ key_metrics }}"),
            ("h2", "（二）重点工作进展"),
            ("slot", "{{ work_progress }}"),
            ("h1", "三、问题与风险"),
            ("slot", "{{ risks }}"),
            ("h1", "四、下月计划"),
            ("slot", "{{ next_month_plan }}"),
        ),
        semantics={
            "report_title": "月报的完整标题，通常包含月份与汇报主体，"
            "例如“2026年9月运营月报”",
            "monthly_overview": "本月整体工作或经营概况的概述",
            "key_metrics": "本月核心指标数据及其环比、同比变化",
            "work_progress": "本月重点工作的推进情况与节点完成状态",
            "risks": "当前面临的主要问题、风险及其影响",
            "next_month_plan": "下月工作目标、计划安排与所需资源支持",
        },
    ),
    TemplateSpec(
        template_id="research-report",
        display_name="调研报告",
        description="通用调研报告模板：背景目的、对象方法、结果发现、结论建议。",
        blocks=(
            ("title", "{{ report_title }}"),
            ("h1", "一、调研背景与目的"),
            ("slot", "{{ background }}"),
            ("h1", "二、调研对象与方法"),
            ("slot", "{{ methodology }}"),
            ("h1", "三、调研结果"),
            ("h2", "（一）主要发现"),
            ("slot", "{{ findings }}"),
            ("h2", "（二）结果分析"),
            ("slot", "{{ analysis }}"),
            ("h1", "四、结论与建议"),
            ("slot", "{{ conclusions }}"),
        ),
        semantics={
            "report_title": "调研报告的完整标题，通常包含调研主题，"
            "例如“关于用户需求的调研报告”",
            "background": "本次调研的背景、目的与意义",
            "methodology": "调研对象、样本范围、调研方法与实施过程",
            "findings": "调研获得的主要事实、数据与发现",
            "analysis": "对调研结果的归纳、对比与分析",
            "conclusions": "基于调研结果得出的结论与可操作的建议",
        },
    ),
)


# ------------------------------------------------------------------- docx build


def _repack_deterministic(docx_bytes: bytes) -> bytes:
    """Rewrite the zip with fixed entry order and timestamps.

    python-docx stamps zip entries with the current time, so a rebuild would
    change asset bytes even when no spec changed — and the seed's content
    digests would then report "changed" on every regeneration. Mirrors
    ``tests/fixtures/weekly_report.py`` (D20).
    """
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


def build_docx(spec: TemplateSpec) -> bytes:
    """Render one template spec into deterministic DOCX bytes."""
    doc = Document()
    _apply_report_styles(doc)
    for kind, text in spec.blocks:
        doc.add_paragraph(text, style=_STYLE_FOR_KIND[kind])
    buffer = io.BytesIO()
    doc.save(buffer)
    return _repack_deterministic(buffer.getvalue())


# --------------------------------------------------------------- report styling

#: 字号（磅）：二号标题 / 三号一级 / 四号二级 / 小四正文
_TITLE_PT = 22
_H1_PT = 16
_H2_PT = 14
_BODY_PT = 12
#: 2 字符首行缩进（小四正文下一个字符约 12 磅）
_INDENT_PT = 24


def _set_font(style: Any, *, east_asia: str, latin: str, size: float, bold: bool | None) -> None:
    """显式字体覆盖主题字体：直接写 ascii/hAnsi/eastAsia 并删主题属性。

    主题字体（asciiTheme 等）随安装的主题漂移且在部分渲染器里回退不可预期；
    模板要的是确定的中文字体面貌。
    """
    font = style.font
    font.size = Pt(size)
    if bold is not None:
        font.bold = bold
    rfonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), east_asia)
    for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        if rfonts.get(qn(f"w:{attr}")) is not None:
            del rfonts.attrib[qn(f"w:{attr}")]


def _force_black(style: Any) -> None:
    """标题用黑色：显式颜色值优先于主题颜色，避免默认的蓝色标题。"""
    style.font.color.rgb = RGBColor(0, 0, 0)
    color = style.element.get_or_add_rPr().find(qn("w:color"))
    if color is not None:
        for attr in ("themeColor", "themeShade", "themeTint"):
            if color.get(qn(f"w:{attr}")) is not None:
                del color.attrib[qn(f"w:{attr}")]


def _apply_report_styles(doc: Document) -> None:
    """中式报告版式：大标题居中黑体加粗，一二级标题黑体加粗，正文宋体首行缩进。

    全部在样式定义层（styles.xml）完成，不触碰正文段落的 XML——槽位占位
    run 保持单 run 纯文本，静态区域的文本摘要不受排版影响，渲染器/门均无感。
    """
    _set_font(
        doc.styles["Normal"], east_asia="宋体", latin="Times New Roman", size=_BODY_PT, bold=None
    )
    normal = doc.styles["Normal"].paragraph_format
    normal.first_line_indent = Pt(_INDENT_PT)
    normal.line_spacing = 1.5
    normal.space_after = Pt(0)

    _set_font(
        doc.styles["Title"], east_asia="黑体", latin="Times New Roman", size=_TITLE_PT, bold=True
    )
    title = doc.styles["Title"].paragraph_format
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.first_line_indent = Pt(0)  # 覆盖 Normal 的继承：居中大标题不缩进
    title.space_before = Pt(0)
    title.space_after = Pt(12)
    title.line_spacing = 1.5
    _force_black(doc.styles["Title"])

    _set_font(
        doc.styles["Heading 1"],
        east_asia="黑体",
        latin="Times New Roman",
        size=_H1_PT,
        bold=True,
    )
    h1 = doc.styles["Heading 1"].paragraph_format
    h1.first_line_indent = Pt(0)
    h1.space_before = Pt(12)
    h1.space_after = Pt(6)
    h1.line_spacing = 1.5
    _force_black(doc.styles["Heading 1"])

    _set_font(
        doc.styles["Heading 2"],
        east_asia="黑体",
        latin="Times New Roman",
        size=_H2_PT,
        bold=True,
    )
    h2 = doc.styles["Heading 2"].paragraph_format
    h2.first_line_indent = Pt(0)
    h2.space_before = Pt(8)
    h2.space_after = Pt(4)
    h2.line_spacing = 1.5
    _force_black(doc.styles["Heading 2"])


# --------------------------------------------------------------- pre-validation


def validate(spec: TemplateSpec, docx_bytes: bytes) -> None:
    """Run the real scaffold+contract pipeline on a throwaway OMAS_HOME.

    Raises AssertionError with the recorded findings when the template would
    not be activatable, so a bad asset never reaches the shipped package.
    """
    from omas.artifacts.store import ArtifactStore
    from omas.storage.db import Ledger
    from omas.templates.scaffold import scaffold_and_register

    declared = [text[2:-2].strip() for kind, text in spec.blocks if kind in ("title", "slot")]
    assert declared == list(spec.semantics), (
        f"{spec.template_id}: placeholder slots {declared} do not match "
        f"semantics keys {list(spec.semantics)}"
    )
    home = Path(tempfile.mkdtemp(prefix="omas-builtin-"))
    try:
        ledger = Ledger.open(home / "ledger.sqlite3")
        try:
            result = scaffold_and_register(
                store=ArtifactStore(home),
                ledger=ledger,
                docx_bytes=docx_bytes,
                template_id=spec.template_id,
                display_name=spec.display_name,
                description=spec.description,
                slot_semantics=dict(spec.semantics),
            )
        finally:
            ledger.close()
        assert result.activatable, (
            f"{spec.template_id}: not activatable, findings: {list(result.findings)}"
        )
        assert sorted(result.slots) == sorted(spec.semantics), (
            f"{spec.template_id}: discovered slots {list(result.slots)} "
            f"!= declared {sorted(spec.semantics)}"
        )
        print(
            f"  {spec.template_id}: v{result.version} activatable=True "
            f"slots={len(result.slots)}"
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ----------------------------------------------------------------------- main


def main() -> None:
    print("building built-in templates:")
    for spec in TEMPLATES:
        docx_bytes = build_docx(spec)
        validate(spec, docx_bytes)
        (BUILTIN_DIR / spec.docx_name).write_bytes(docx_bytes)

    manifest = [
        {
            "template_id": spec.template_id,
            "display_name": spec.display_name,
            "description": spec.description,
            "docx": spec.docx_name,
            "slot_semantics": spec.semantics,
        }
        for spec in TEMPLATES
    ]
    (BUILTIN_DIR / "builtin-templates.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(TEMPLATES)} templates + builtin-templates.json to {BUILTIN_DIR}")


if __name__ == "__main__":
    main()
