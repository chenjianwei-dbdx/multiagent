"""FormatGate — structure-level style verification (Master §19; v1.1 §3.3).

Checks the rendered candidate's *effective* styles (direct formatting →
style chain → docDefaults, theme-indirect counts as unknown) against the
template's StyleSpec. Assertions the resolver cannot decide come back
``unknown`` — which is NOT pass and blocks finalize (v1.1 §2).

This gate proves OOXML properties satisfy the contract; it does not promise
pagination or visual fidelity (v1.1 §3.3).
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from omas.core.digest import canonical_model_sha
from omas.docx._xml import parse_xml
from omas.docx.extract import ExtractedPart, extract_details
from omas.docx.styles import StyleResolver
from omas.domain.findings import CheckResult, GateName, GateReport, GateStatus
from omas.domain.ir import DocxRenderIR
from omas.domain.template import StyleSpec, TemplateContract
from omas.gates.gate_b import SlotLocations

_RULE = "format_gate/1"
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class FormatGate:
    def run(
        self,
        *,
        candidate_docx: bytes,
        candidate_sha256: str,
        contract: TemplateContract,
        styles_spec: dict[str, StyleSpec],
        ir: DocxRenderIR,
        locations: SlotLocations,
    ) -> GateReport:
        results: list[CheckResult] = []
        resolver = StyleResolver(candidate_docx)
        detail = extract_details(candidate_docx)

        for slot in ir.slots:
            spec = styles_spec.get(slot.style_key)
            if spec is None:
                results.append(
                    CheckResult(
                        check_id=f"style:{slot.slot_id}",
                        rule_id=_RULE,
                        status=GateStatus.UNKNOWN,
                        locator=f"style_key {slot.style_key}",
                        actual="no StyleSpec declared for this style key",
                    )
                )
                continue
            for part_name, index in locations.slots.get(slot.slot_id, ()):
                part = detail.part(part_name)
                if part is None or index >= len(part.paragraphs):
                    results.append(
                        CheckResult(
                            check_id=f"style:{slot.slot_id}:{index}",
                            rule_id=_RULE,
                            status=GateStatus.FAIL,
                            locator=f"{part_name}:{index}",
                            actual="located paragraph missing",
                        )
                    )
                    continue
                para_detail = part.paragraphs[index]
                results.append(
                    _check_paragraph_style(
                        resolver,
                        part.to_extracted(),
                        index,
                        slot.slot_id,
                        spec,
                        _direct_indent_twips(para_detail.element),
                    )
                )

        margin_specs = [
            s
            for s in styles_spec.values()
            if s.margin_top_twips is not None or s.margin_bottom_twips is not None
        ]
        if margin_specs:
            results.append(_check_margins(candidate_docx, margin_specs[0]))

        return GateReport(
            gate=GateName.FORMAT_GATE,
            candidate_sha256=candidate_sha256,
            render_ir_sha256=canonical_model_sha(ir),
            template_version_id=ir.template_version_id,
            rule_version=_RULE,
            results=tuple(results),
        )


def _direct_indent_twips(element: Any) -> int | None:
    """Direct w:ind/@w:left on the paragraph (twips), if present."""
    if element is None:
        return None
    ppr = element.find(f"{{{_W_NS}}}pPr")
    if ppr is None:
        return None
    ind = ppr.find(f"{{{_W_NS}}}ind")
    if ind is None:
        return None
    value = ind.get(f"{{{_W_NS}}}left")
    return int(value) if value is not None else None


def _check_paragraph_style(
    resolver: StyleResolver,
    part: ExtractedPart,
    index: int,
    slot_id: str,
    spec: StyleSpec,
    direct_indent_twips: int | None = None,
) -> CheckResult:
    resolved = resolver.resolve_paragraph(part, index)
    problems: list[str] = []
    unknowns: list[str] = []

    if spec.paragraph_style is not None and resolved.paragraph_style is None:
        unknowns.append("paragraph_style unresolved")
    if spec.indent_left_twips is not None:
        effective_indent = (
            spec.indent_left_twips if direct_indent_twips is None else direct_indent_twips
        )
        if abs(effective_indent - spec.indent_left_twips) > spec.indent_tolerance_twips:
            problems.append(
                f"indent_left {effective_indent} outside tolerance of "
                f"{spec.indent_left_twips}±{spec.indent_tolerance_twips}"
            )
    if (
        spec.numbering_id is None
        and spec.numbering_level is None
        and resolved.numbering_id is not None
    ):
        problems.append(
            f"undeclared numbering {resolved.numbering_id} on a slot styled {spec.style_key}"
        )
    if spec.font_latin is not None:
        if resolved.font_latin is None:
            unknowns.append("font_latin unresolved (theme-indirect)")
        elif resolved.font_latin != spec.font_latin:
            problems.append(f"font_latin {resolved.font_latin!r} != {spec.font_latin!r}")
    if spec.font_east_asia is not None:
        if resolved.font_east_asia is None:
            unknowns.append("font_east_asia unresolved (theme-indirect)")
        elif resolved.font_east_asia != spec.font_east_asia:
            problems.append(
                f"font_east_asia {resolved.font_east_asia!r} != {spec.font_east_asia!r}"
            )
    if spec.font_size_half_points is not None:
        if resolved.font_size_half_points is None:
            unknowns.append("font_size unresolved")
        elif abs(resolved.font_size_half_points - spec.font_size_half_points) > (
            spec.font_size_tolerance_half_points
        ):
            problems.append(
                f"font_size {resolved.font_size_half_points} outside tolerance of "
                f"{spec.font_size_half_points}±{spec.font_size_tolerance_half_points}"
            )
    if spec.bold is not None and resolved.bold is not None and resolved.bold != spec.bold:
        problems.append(f"bold {resolved.bold} != {spec.bold}")
    if spec.alignment is not None and spec.alignment != "unknown":
        if resolved.alignment is None:
            unknowns.append("alignment unresolved")
        elif resolved.alignment != spec.alignment:
            problems.append(f"alignment {resolved.alignment!r} != {spec.alignment!r}")

    if problems:
        status = GateStatus.FAIL
        detail = "; ".join(problems)
    elif unknowns:
        status = GateStatus.UNKNOWN
        detail = "; ".join(unknowns)
    else:
        status = GateStatus.PASS
        detail = None
    return CheckResult(
        check_id=f"style:{slot_id}:{index}",
        rule_id=_RULE,
        status=status,
        locator=f"document:{index}",
        actual=detail,
    )


def _check_margins(candidate_docx: bytes, spec: StyleSpec) -> CheckResult:
    """Section top/bottom margins against the spec, in twips."""
    with zipfile.ZipFile(io.BytesIO(candidate_docx)) as zf:
        document = zf.read("word/document.xml")
    root = parse_xml(document, source="word/document.xml")
    ns = {"w": _W_NS}
    sect = root.find(".//w:body/w:sectPr", ns)
    if sect is None:
        return CheckResult(
            check_id="section_margins",
            rule_id=_RULE,
            status=GateStatus.UNKNOWN,
            actual="no sectPr found",
        )
    mar = sect.find("w:pgMar", ns)
    if mar is None:
        return CheckResult(
            check_id="section_margins",
            rule_id=_RULE,
            status=GateStatus.UNKNOWN,
            actual="no pgMar in sectPr",
        )
    problems: list[str] = []
    top = mar.get(f"{{{_W_NS}}}top")
    bottom = mar.get(f"{{{_W_NS}}}bottom")
    if (
        spec.margin_top_twips is not None
        and top is not None
        and abs(int(top) - spec.margin_top_twips) > spec.indent_tolerance_twips
    ):
        problems.append(f"margin_top {top} != {spec.margin_top_twips}")
    if (
        spec.margin_bottom_twips is not None
        and bottom is not None
        and abs(int(bottom) - spec.margin_bottom_twips) > spec.indent_tolerance_twips
    ):
        problems.append(f"margin_bottom {bottom} != {spec.margin_bottom_twips}")
    return CheckResult(
        check_id="section_margins",
        rule_id=_RULE,
        status=GateStatus.FAIL if problems else GateStatus.PASS,
        actual="; ".join(problems) or None,
    )
