"""Format gate benchmark: classification metrics against gold labels (v1.1 §11).

MVP bar: every case in the supported subset classified per gold, required
unknown == 0 on clean cases; per-class metrics reported with sample counts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures.weekly_report import build_weekly_report_template
from omas.domain.findings import GateStatus
from omas.domain.ids import TaskId, TemplateVersionId
from omas.domain.ir import DocxRenderIR, RenderSlot, SourceSpanRef, TemplateStaticRef
from omas.gates.format import FormatGate
from omas.gates.gate_b import ProvenanceGate, SlotLocations

from .format_benchmark import build_corpus

pytestmark = pytest.mark.benchmark


def _ir() -> DocxRenderIR:
    h = "a" * 64
    span = SourceSpanRef(
        artifact_id="art_" + "1" * 32, canonical_sha256=h, start=0, end=2, span_sha256=h
    )
    return DocxRenderIR(
        schema_version=1,
        task_id=TaskId("task_" + "2" * 32),
        epoch=1,
        template_version_id=TemplateVersionId("tver_" + "3" * 32),
        template_docx_artifact_id="art_" + "1" * 32,
        plan_artifact_id="art_" + "1" * 32,
        binding_artifact_id="art_" + "1" * 32,
        static_regions=(
            TemplateStaticRef(
                template_version_id=TemplateVersionId("tver_" + "3" * 32), region_id="title"
            ),
        ),
        slots=(
            RenderSlot(slot_id="sales_summary", spans=(span,), style_key="body"),
            RenderSlot(slot_id="risks", spans=(span,), style_key="body"),
            RenderSlot(slot_id="next_plan", spans=(span,), style_key="body"),
        ),
    )


def _classify(case) -> tuple[str, str]:
    """Return (predicted, note): predicted in {fail, unknown, pass}."""
    fixture = build_weekly_report_template()
    from omas.templates.extractor import build_contract

    contract = build_contract(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
        version=1,
        extractor_version="bench",
    )
    ir = _ir()
    resolver = lambda ref: "基准渲染段落内容，包含中文与数字 123。"  # noqa: E731
    # provenance gate first: content/structure deviations are its jurisdiction
    provenance = ProvenanceGate().run(
        candidate_docx=case.docx_bytes,
        template_docx=fixture.docx_bytes,
        contract=contract,
        ir=ir,
        resolve_span=resolver,
        static_map=fixture.static_map,
    )
    provenance_status = provenance.overall_status()
    if provenance_status is GateStatus.FAIL:
        return "fail", "provenance"
    import hashlib

    locations = SlotLocations(
        {
            "sales_summary": (("document", 4),),
            "risks": (("document", 7),),
            "next_plan": (("document", 9),),
        }
    )
    from omas.domain.template import StyleSpec

    styles = {
        key: StyleSpec.model_validate({**value, "style_key": key})
        for key, value in fixture.styles_spec.items()
    }
    # margins spec present? keep spec as authored; locate slots dynamically
    from omas.gates.gate_b import classify_output

    located = classify_output(
        template_docx=fixture.docx_bytes,
        candidate_docx=case.docx_bytes,
        contract=contract,
        ir=ir,
        resolve_span=resolver,
    )[0]
    locations = located if located.slots else locations
    report = FormatGate().run(
        candidate_docx=case.docx_bytes,
        candidate_sha256=hashlib.sha256(case.docx_bytes).hexdigest(),
        contract=contract,
        styles_spec=styles,
        ir=ir,
        locations=locations,
    )
    overall = report.overall_status()
    return ("pass" if overall is GateStatus.PASS else overall.value), "format"


def test_format_benchmark_meets_mvp_bar() -> None:
    cases = build_corpus()
    clean = [c for c in cases if not c.expect_fail]
    injected = [c for c in cases if c.expect_fail]
    assert len(clean) >= 20 and len(injected) >= 20

    tp = fp = fn = tn = 0
    unknown_on_clean = 0
    unknown_on_injected = 0
    per_class: dict[str, dict[str, int]] = {}
    misclassified: list[str] = []
    for case in cases:
        predicted, _gate = _classify(case)
        flagged = predicted in ("fail", "unknown")
        if case.expect_fail and predicted == "fail":
            tp += 1
        elif case.expect_fail and predicted == "unknown":
            tp += 1  # positive unknown counts as detected-not-passed, tracked
            unknown_on_injected += 1
        elif case.expect_fail:
            fn += 1
            misclassified.append(f"{case.case_id}: gold=fail got={predicted}")
        elif predicted == "pass":
            tn += 1
        elif predicted == "unknown":
            unknown_on_clean += 1
            misclassified.append(f"{case.case_id}: gold=clean got=unknown")
        else:
            fp += 1
            misclassified.append(f"{case.case_id}: gold=clean got=fail")
        bucket = per_class.setdefault(case.label, {"n": 0, "detected": 0})
        bucket["n"] += 1
        if case.expect_fail == flagged:
            bucket["detected"] += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    print(
        f"\nformat benchmark: cases={len(cases)} tp={tp} fp={fp} fn={fn} tn={tn} "
        f"precision={precision:.2f} recall={recall:.2f} fpr={fpr:.2f} "
        f"unknown_clean={unknown_on_clean} unknown_injected={unknown_on_injected}"
    )
    for label, bucket in sorted(per_class.items()):
        print(f"  class {label}: {bucket['detected']}/{bucket['n']} per gold")

    # MVP bar: everything classified per gold; no unknown on clean
    assert not misclassified, misclassified
    assert unknown_on_clean == 0
    assert recall == 1.0 and precision == 1.0
