"""Format benchmark corpus: 20 clean + 20 injected with gold labels.

Eight injection classes (v1.1 §11): font, font size, alignment, numbering,
margins, indentation, style drift, placeholder residue. Each mutation is
applied to a *rendered* candidate so the gates see a realistic document.
"""

from __future__ import annotations

import io
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures.weekly_report import build_weekly_report_template

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    label: str  # "clean" | injection class
    expect_fail: bool
    docx_bytes: bytes


def _render_clean(template_fixture) -> bytes:
    from omas.domain.ir import DocxRenderIR, RenderSlot
    from omas.domain.spans import SourceSpanRef
    from omas.renderers.docx_renderer import render_docx
    from omas.templates.extractor import build_contract

    fixture = template_fixture
    contract = build_contract(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
        version=1,
        extractor_version="bench",
    )
    h = "a" * 64
    spans = lambda: (  # noqa: E731
        SourceSpanRef(
            artifact_id="art_" + "1" * 32,
            canonical_sha256=h,
            start=0,
            end=2,
            span_sha256=h,
        ),
    )
    ir = DocxRenderIR(
        schema_version=1,
        task_id="task_" + "2" * 32,
        epoch=1,
        template_version_id="tver_" + "3" * 32,
        template_docx_artifact_id="art_" + "1" * 32,
        plan_artifact_id="art_" + "1" * 32,
        binding_artifact_id="art_" + "1" * 32,
        static_regions=(),
        slots=(
            RenderSlot(slot_id="sales_summary", spans=spans(), style_key="body"),
            RenderSlot(slot_id="risks", spans=spans(), style_key="body"),
            RenderSlot(slot_id="next_plan", spans=spans(), style_key="body"),
        ),
    )
    resolver = lambda ref: "基准渲染段落内容，包含中文与数字 123。"  # noqa: E731
    return render_docx(
        ir=ir, contract=contract, template_docx=fixture.docx_bytes, resolve_span=resolver
    )


def _mutate(docx_bytes: bytes, mutation: str) -> bytes:
    """Apply one style mutation to the first slot paragraph, return new bytes."""
    import lxml.etree as etree

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        names = zf.namelist()
        payload = {name: zf.read(name) for name in names}
    root = etree.fromstring(
        payload["word/document.xml"],
        parser=etree.XMLParser(resolve_entities=False, no_network=True),
    )
    paragraphs = root.findall(f".//{_W}body/{_W}p")
    target = None
    for paragraph in paragraphs:
        if paragraph.find(f"{_W}bookmarkStart") is not None:
            names_attr = {b.get(f"{_W}name") for b in paragraph.findall(f"{_W}bookmarkStart")}
            if "sales_summary" in names_attr:
                target = paragraph
                break
    if target is None:  # fallback: first body paragraph after the headings
        target = paragraphs[4] if len(paragraphs) > 4 else paragraphs[0]

    def _rpr(run: object) -> object:
        rpr = run.find(f"{_W}rPr")
        if rpr is None:
            rpr = etree.SubElement(run, f"{_W}rPr")
        return rpr

    runs = target.findall(f"{_W}r")
    if mutation == "font":
        for run in runs:
            rpr = _rpr(run)
            fonts = rpr.find(f"{_W}rFonts")
            if fonts is None:
                fonts = etree.SubElement(rpr, f"{_W}rFonts")
            fonts.set(f"{_W}ascii", "InjectedFont")
            fonts.set(f"{_W}eastAsia", "InjectedFont")
    elif mutation == "font_size":
        for run in runs:
            rpr = _rpr(run)
            size = rpr.find(f"{_W}sz")
            if size is None:
                size = etree.SubElement(rpr, f"{_W}sz")
            size.set(f"{_W}val", "44")
    elif mutation == "alignment":
        ppr = target.find(f"{_W}pPr")
        if ppr is None:
            ppr = etree.SubElement(target, f"{_W}pPr")
        jc = ppr.find(f"{_W}jc")
        if jc is None:
            jc = etree.SubElement(ppr, f"{_W}jc")
        jc.set(f"{_W}val", "center")
    elif mutation == "numbering":
        ppr = target.find(f"{_W}pPr")
        if ppr is None:
            ppr = etree.SubElement(target, f"{_W}pPr")
        numpr = etree.SubElement(ppr, f"{_W}numPr")
        etree.SubElement(numpr, f"{_W}numId").set(f"{_W}val", "1")
        etree.SubElement(numpr, f"{_W}ilvl").set(f"{_W}val", "0")
    elif mutation == "indent":
        ppr = target.find(f"{_W}pPr")
        if ppr is None:
            ppr = etree.SubElement(target, f"{_W}pPr")
        ind = ppr.find(f"{_W}ind")
        if ind is None:
            ind = etree.SubElement(ppr, f"{_W}ind")
        ind.set(f"{_W}left", "4320")
    elif mutation == "style_drift":
        ppr = target.find(f"{_W}pPr")
        if ppr is None:
            ppr = etree.SubElement(target, f"{_W}pPr")
        style = ppr.find(f"{_W}pStyle")
        if style is None:
            style = etree.SubElement(ppr, f"{_W}pStyle")
        style.set(f"{_W}val", "Heading2")
    elif mutation == "placeholder_residue":
        for run in runs:
            texts = run.findall(f"{_W}t")
            for t in texts:
                t.text = "{{ injected }}"
                break
            break
    elif mutation == "margins":
        sect = root.find(f".//{_W}body/{_W}sectPr")
        if sect is not None:
            mar = sect.find(f"{_W}pgMar")
            if mar is not None:
                mar.set(f"{_W}top", "100")
    payload["word/document.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.writestr(name, payload[name])
    return buffer.getvalue()


_CLASSES = (
    "font",
    "font_size",
    "alignment",
    "numbering",
    "indent",
    "style_drift",
    "placeholder_residue",
    "margins",
)


def build_corpus() -> list[Case]:
    fixture = build_weekly_report_template()
    clean = _render_clean(fixture)
    cases: list[Case] = []
    for index in range(20):
        cases.append(Case(f"clean-{index:02d}", "clean", False, clean))
    for round_index in range(3):  # ≥2 per class across 8 classes = 24 injected
        for mutation in _CLASSES:
            cases.append(
                Case(
                    f"injected-{mutation}-{round_index}",
                    mutation,
                    True,
                    _mutate(clean, mutation),
                )
            )
    return cases
