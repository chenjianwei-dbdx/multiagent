"""Gate B — post-render provenance verification (v1.1 §7; T13/T02/T03).

Strategy: rebuild the *entire expected document* from the template plus the
IR's span texts, then compare the rendered candidate paragraph by paragraph,
token by token. This proves three things at once:

- every dynamic paragraph equals its span's exact text (no rewriting, no
  filler, no LLM-injected prose — the renderer has no free-text API, this is
  the independent second lock);
- every static paragraph is byte-identical to the template version (any text
  inserted outside slots breaks the sequence — T13);
- nothing extra exists: paragraph counts must match the expansion exactly.

Header/footer parts must be entirely unchanged (no dynamic slots allowed
there). Unsupported text containers appearing in the output fail outright.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from omas.core.digest import canonical_model_sha
from omas.docx.extract import (
    extract_details,
    find_unsupported_text_containers,
    paragraph_plain_text,
)
from omas.domain.errors import OmasError
from omas.domain.findings import CheckResult, GateName, GateReport, GateStatus
from omas.domain.ir import DocxRenderIR
from omas.domain.spans import SourceSpanRef
from omas.domain.template import TemplateContract
from omas.templates.extractor import recompute_static_regions

_RULE = "provenance_gate/1"

SpanTextResolver = Callable[[SourceSpanRef], str]


@dataclass(frozen=True, slots=True)
class SlotLocations:
    """slot_id -> ordered (part_name, paragraph_index) of rendered content."""

    slots: dict[str, tuple[tuple[str, int], ...]]


def classify_output(
    *,
    template_docx: bytes,
    candidate_docx: bytes,
    contract: TemplateContract,
    ir: DocxRenderIR,
    resolve_span: SpanTextResolver,
) -> tuple[SlotLocations, list[str]]:
    """Walk template and output in parallel; return slot locations + problems.

    The template's placeholder paragraphs expand to the IR's span sequence
    (one output paragraph per span; overridden slots render one empty
    paragraph). Every other paragraph must match exactly.
    """
    template_detail = extract_details(template_docx)
    candidate_detail = extract_details(candidate_docx)
    problems: list[str] = []
    locations: dict[str, tuple[tuple[str, int], ...]] = {}

    placeholder_to_slot = {spec.placeholder.strip(): spec.slot_id for spec in contract.slots}
    expected_texts: dict[str, list[str]] = {}
    for slot in ir.slots:
        if slot.override_artifact_id is not None:
            expected_texts[slot.slot_id] = [""]
        else:
            texts: list[str] = []
            for span in slot.spans:
                try:
                    texts.append(resolve_span(span))
                except OmasError as exc:
                    problems.append(f"span resolution failed for {slot.slot_id}: {exc.code}")
            expected_texts[slot.slot_id] = texts

    template_part = template_detail.part("document")
    candidate_part = candidate_detail.part("document")
    if template_part is None or candidate_part is None:
        return SlotLocations({}), ["document part missing in template or candidate"]

    template_paras = [paragraph_plain_text(d.paragraph) for d in template_part.paragraphs]
    candidate_paras = [paragraph_plain_text(d.paragraph) for d in candidate_part.paragraphs]

    i = j = 0
    while i < len(template_paras) and j < len(candidate_paras):
        slot_id = placeholder_to_slot.get(template_paras[i].strip())
        if slot_id is None:
            if template_paras[i] != candidate_paras[j]:
                problems.append(
                    f"static paragraph {j} changed: "
                    f"expected {template_paras[i]!r:.60}, got {candidate_paras[j]!r:.60}"
                )
            i += 1
            j += 1
            continue
        expected = expected_texts.get(slot_id)
        if expected is None:
            problems.append(f"template placeholder for {slot_id} has no IR slot")
            i += 1
            continue
        found: list[tuple[str, int]] = []
        for text in expected:
            if j >= len(candidate_paras):
                problems.append(f"document ended inside slot {slot_id}")
                break
            if candidate_paras[j] != text:
                problems.append(
                    f"slot {slot_id} paragraph {j} mismatch: "
                    f"expected {text!r:.60}, got {candidate_paras[j]!r:.60}"
                )
            found.append(("document", j))
            j += 1
        locations[slot_id] = tuple(found)
        i += 1

    if i != len(template_paras):
        problems.append(f"template not fully consumed at paragraph {i}")
    if j != len(candidate_paras):
        problems.append(
            f"candidate has {len(candidate_paras) - j} extra paragraph(s) beyond the expectation"
        )

    # header/footer parts: no dynamic content allowed, compare whole part text
    for part in template_detail.parts:
        if part.name == "document":
            continue
        rendered = candidate_detail.part(part.name)
        template_text = "\n".join(paragraph_plain_text(d.paragraph) for d in part.paragraphs)
        rendered_text = (
            "\n".join(paragraph_plain_text(d.paragraph) for d in rendered.paragraphs)
            if rendered is not None
            else None
        )
        if rendered_text != template_text:
            problems.append(f"part {part.name} differs from the template version")
    for part in candidate_detail.parts:
        if part.name != "document" and template_detail.part(part.name) is None:
            problems.append(f"candidate introduces new part {part.name}")

    return SlotLocations(locations), problems


class ProvenanceGate:
    """Gate B runner over a rendered candidate."""

    def run(
        self,
        *,
        candidate_docx: bytes,
        template_docx: bytes,
        contract: TemplateContract,
        ir: DocxRenderIR,
        resolve_span: SpanTextResolver,
        static_map: dict[str, object] | None = None,
    ) -> GateReport:
        results: list[CheckResult] = []

        locations, problems = classify_output(
            template_docx=template_docx,
            candidate_docx=candidate_docx,
            contract=contract,
            ir=ir,
            resolve_span=resolve_span,
        )
        results.append(
            CheckResult(
                check_id="content_sequence",
                rule_id=_RULE,
                status=GateStatus.FAIL if problems else GateStatus.PASS,
                locator="document",
                actual="; ".join(problems[:5]) or None,
            )
        )

        # per-slot evidence (already covered by the sequence, reported granularly)
        for slot in ir.slots:
            found = locations.slots.get(slot.slot_id, ())
            expected_count = 1 if slot.override_artifact_id is not None else len(slot.spans)
            ok = len(found) == expected_count
            results.append(
                CheckResult(
                    check_id=f"slot_placement:{slot.slot_id}",
                    rule_id=_RULE,
                    status=GateStatus.PASS if ok else GateStatus.FAIL,
                    locator=f"paragraphs {[idx for _, idx in found]}",
                    expected=f"{expected_count} paragraph(s)",
                    actual=f"{len(found)}",
                )
            )

        # placeholder residue anywhere in the document part
        residue = _placeholder_residue(candidate_docx)
        results.append(
            CheckResult(
                check_id="placeholder_residue",
                rule_id=_RULE,
                status=GateStatus.FAIL if residue else GateStatus.PASS,
                actual=residue or None,
            )
        )

        # unsupported text containers must not appear in the output
        unsupported = find_unsupported_text_containers(candidate_docx)
        results.append(
            CheckResult(
                check_id="unsupported_containers",
                rule_id=_RULE,
                status=GateStatus.FAIL if unsupported else GateStatus.PASS,
                actual=(
                    "; ".join(f"{f.code}@{f.locator}" for f in unsupported[:5]) or None
                ),
            )
        )

        # anchors survived the render (structure identity for located slots)
        anchor_problems = _check_anchors(candidate_docx, contract, locations)
        results.append(
            CheckResult(
                check_id="anchors",
                rule_id=_RULE,
                status=GateStatus.FAIL if anchor_problems else GateStatus.PASS,
                actual="; ".join(anchor_problems[:5]) or None,
            )
        )

        # static region digests recomputed from the template must equal the
        # contract's recorded digests (contract integrity, v1.1 §7 step 4)
        if static_map is not None:
            findings, regions = recompute_static_regions(template_docx, static_map)
            contract_regions = {r.region_id: r for r in contract.static_regions}
            region_problems = [f"recompute finding: {f.code}" for f in findings]
            for region in regions:
                recorded = contract_regions.get(region.region_id)
                if recorded is None:
                    region_problems.append(f"region {region.region_id} unknown to contract")
                elif recorded.text_sha256 != region.text_sha256:
                    region_problems.append(f"region {region.region_id} digest drift")
            results.append(
                CheckResult(
                    check_id="static_region_integrity",
                    rule_id=_RULE,
                    status=GateStatus.FAIL if region_problems else GateStatus.PASS,
                    actual="; ".join(region_problems[:5]) or None,
                )
            )

        import hashlib

        candidate_sha = hashlib.sha256(candidate_docx).hexdigest()
        return GateReport(
            gate=GateName.PROVENANCE_GATE,
            candidate_sha256=candidate_sha,
            render_ir_sha256=canonical_model_sha(ir),
            template_version_id=ir.template_version_id,
            rule_version=_RULE,
            results=tuple(results),
        )


def _placeholder_residue(docx: bytes) -> str | None:
    detail = extract_details(docx)
    for part in detail.parts:
        for para in part.paragraphs:
            text = paragraph_plain_text(para.paragraph)
            if "{{" in text or "{%" in text:
                return f"{part.name}:{text!r:.50}"
    return None


def _check_anchors(
    candidate_docx: bytes, contract: TemplateContract, locations: SlotLocations
) -> list[str]:
    detail = extract_details(candidate_docx)
    document = detail.part("document")
    if document is None:
        return ["document part missing"]
    problems: list[str] = []
    for spec in contract.slots:
        anchor = spec.anchor
        if anchor is None:
            continue
        found = locations.slots.get(spec.slot_id, ())
        if not found:
            continue  # placement check reports this
        part_name, index = found[0]
        part = detail.part(part_name)
        if part is None or index >= len(part.paragraphs):
            problems.append(f"{spec.slot_id}: located paragraph out of range")
            continue
        # anchor identity matches either the bookmark name or its numeric id
        identifiers = set()
        for bookmark in part.paragraphs[index].bookmarks:
            identifiers.add(bookmark.name)
            identifiers.add(bookmark.id)
        if anchor.bookmark_id not in identifiers:
            problems.append(f"{spec.slot_id}: anchor {anchor.bookmark_id} missing on output")
    return problems
