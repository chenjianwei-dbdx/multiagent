"""Gate A — provenance precheck before render (v1.1 §7).

Verifies every dynamic-span source, template identity, task scope, binding
freshness and override refs against the live ledger + file pool. Runs before
``render_docx``; any required FAIL blocks rendering entirely (I2/I7).
"""

from __future__ import annotations

from collections.abc import Sequence

from omas.artifacts.store import ArtifactStore
from omas.core.canonical import canonicalize, sha256_text
from omas.core.digest import canonical_model_sha, material_set_digest
from omas.domain.artifact import Artifact
from omas.domain.errors import OmasError
from omas.domain.findings import CheckResult, GateName, GateReport, GateStatus
from omas.domain.ir import BindingIR, BoundBinding, DocxRenderIR, RenderSlot
from omas.domain.task import Task
from omas.domain.template import TemplateContract
from omas.storage.db import Ledger

_RULE = "provenance_precheck/1"


class ProvenancePrecheck:
    """Gate A runner: pure verification, returns a structured report."""

    def __init__(self, ledger: Ledger, store: ArtifactStore) -> None:
        self._ledger = ledger
        self._store = store

    def run(
        self,
        *,
        task: Task,
        ir: DocxRenderIR,
        binding: BindingIR,
        contract: TemplateContract,
        live_materials: Sequence[Artifact],
    ) -> GateReport:
        results: list[CheckResult] = []
        results.append(self._check_template_identity(task, ir, binding, contract))
        results.append(self._check_epoch(task, ir, binding))
        results.extend(self._check_slots(ir))
        results.append(self._check_binding_freshness(binding, live_materials))
        results.append(self._check_static_regions(ir, contract))
        results.extend(self._check_overrides(task, ir))
        return GateReport(
            gate=GateName.PROVENANCE_PRECHECK,
            candidate_sha256=None,
            render_ir_sha256=canonical_model_sha(ir),
            template_version_id=ir.template_version_id,
            rule_version=_RULE,
            results=tuple(results),
        )

    # ------------------------------------------------------------------ checks

    def _check_template_identity(
        self,
        task: Task,
        ir: DocxRenderIR,
        binding: BindingIR,
        contract: TemplateContract,
    ) -> CheckResult:
        version_row = None
        mismatch: str | None = None
        if task.template_version_id != ir.template_version_id:
            mismatch = (
                f"task bound to {task.template_version_id}, IR targets {ir.template_version_id}"
            )
        elif binding.template_version_id != ir.template_version_id:
            mismatch = (
                f"binding targets {binding.template_version_id}, "
                f"IR targets {ir.template_version_id}"
            )
        else:
            version_row = self._ledger.template_versions.get(ir.template_version_id)
            if version_row is None:
                mismatch = f"template version {ir.template_version_id} not registered"
            elif version_row.docx_sha256 != contract.hashes.docx_sha256:
                mismatch = "contract docx hash differs from the registered template version"
        return CheckResult(
            check_id="template_identity",
            rule_id=_RULE,
            status=GateStatus.PASS if mismatch is None else GateStatus.FAIL,
            locator="task/ir/binding/contract",
            actual=mismatch,
        )

    def _check_epoch(self, task: Task, ir: DocxRenderIR, binding: BindingIR) -> CheckResult:
        ok = task.epoch == ir.epoch == binding.epoch
        return CheckResult(
            check_id="epoch_match",
            rule_id=_RULE,
            status=GateStatus.PASS if ok else GateStatus.FAIL,
            expected=f"epoch {task.epoch}",
            actual=f"ir={ir.epoch}, binding={binding.epoch}",
        )

    def _check_slots(self, ir: DocxRenderIR) -> list[CheckResult]:
        results: list[CheckResult] = []
        for slot in ir.slots:
            results.append(self._check_slot_scopes(ir, slot))
            if slot.spans:
                results.append(self._check_slot_spans(ir, slot))
        return results

    def _check_slot_scopes(
        self, ir: DocxRenderIR, slot: RenderSlot
    ) -> CheckResult:
        """Every referenced artifact belongs to this task (T18: cross-task reject)."""
        problems: list[str] = []
        for span in slot.spans:
            artifact = self._ledger.artifacts.get(span.artifact_id)
            if artifact is None:
                problems.append(f"{span.artifact_id}: not registered")
            elif artifact.task_id != ir.task_id:
                problems.append(f"{span.artifact_id}: belongs to task {artifact.task_id}")
            elif artifact.sha256 != span.canonical_sha256:
                problems.append(f"{span.artifact_id}: canonical hash drift")
        return CheckResult(
            check_id=f"scope:{slot.slot_id}",
            rule_id=_RULE,
            status=GateStatus.FAIL if problems else GateStatus.PASS,
            locator=f"slot {slot.slot_id}",
            actual="; ".join(problems) or None,
        )

    def _check_slot_spans(
        self, ir: DocxRenderIR, slot: RenderSlot
    ) -> CheckResult:
        """File bytes verify + span re-verification + control-char policy."""
        problems: list[str] = []
        for span in slot.spans:
            artifact = self._ledger.artifacts.get(span.artifact_id)
            if artifact is None:
                continue  # already reported by scope check
            try:
                raw = self._store.read_verified(artifact.relative_path, artifact.sha256)
            except OmasError as exc:
                problems.append(f"{span.artifact_id}: file verify failed ({exc.code})")
                continue
            canonical = canonicalize(raw)
            if canonical.sha256 != span.canonical_sha256:
                problems.append(f"{span.artifact_id}: canonical hash mismatch after load")
                continue
            slice_text = canonical.text[span.start : span.end]
            if sha256_text(slice_text) != span.span_sha256:
                problems.append(f"[{span.start},{span.end}): span hash mismatch")
            if any(ch in slice_text for ch in ("\x07", "\x0c")) or any(
                ord(ch) < 0x20 and ch not in "\t\n\r" for ch in slice_text
            ):
                problems.append(f"[{span.start},{span.end}): forbidden control characters")
        return CheckResult(
            check_id=f"spans:{slot.slot_id}",
            rule_id=_RULE,
            status=GateStatus.FAIL if problems else GateStatus.PASS,
            locator=f"slot {slot.slot_id}",
            actual="; ".join(problems) or None,
        )

    def _check_binding_freshness(
        self, binding: BindingIR, live_materials: Sequence[Artifact]
    ) -> CheckResult:
        """Stale bindings (material set changed since assembly) must not render."""
        live = material_set_digest((a.artifact_id, a.sha256) for a in live_materials)
        ok = live == binding.material_set_digest
        return CheckResult(
            check_id="binding_freshness",
            rule_id=_RULE,
            status=GateStatus.PASS if ok else GateStatus.FAIL,
            expected=binding.material_set_digest[:12],
            actual=live[:12],
        )

    def _check_static_regions(self, ir: DocxRenderIR, contract: TemplateContract) -> CheckResult:
        """Static refs must target this template version and cover every region."""
        problems: list[str] = []
        for ref in ir.static_regions:
            if ref.template_version_id != ir.template_version_id:
                problems.append(f"region {ref.region_id}: foreign template version")
        contract_regions = {r.region_id for r in contract.static_regions}
        ir_regions = {r.region_id for r in ir.static_regions}
        if contract_regions - ir_regions:
            problems.append(f"uncovered regions: {sorted(contract_regions - ir_regions)}")
        if ir_regions - contract_regions:
            problems.append(f"unknown regions: {sorted(ir_regions - contract_regions)}")
        return CheckResult(
            check_id="static_regions",
            rule_id=_RULE,
            status=GateStatus.FAIL if problems else GateStatus.PASS,
            actual="; ".join(problems) or None,
        )

    def _check_overrides(self, task: Task, ir: DocxRenderIR) -> list[CheckResult]:
        results: list[CheckResult] = []
        active = self._ledger.slot_overrides.active_for_task(task.task_id, task.epoch)
        for slot in ir.slots:
            if slot.override_artifact_id is None:
                continue
            problems: list[str] = []
            if slot.slot_id not in active:
                problems.append("no recorded user omission for this slot/epoch")
            else:
                artifact = self._ledger.artifacts.get(slot.override_artifact_id)
                if artifact is None:
                    problems.append("override artifact not registered")
                elif artifact.task_id != task.task_id:
                    problems.append(f"override artifact belongs to {artifact.task_id}")
            results.append(
                CheckResult(
                    check_id=f"override:{slot.slot_id}",
                    rule_id=_RULE,
                    status=GateStatus.FAIL if problems else GateStatus.PASS,
                    locator=f"slot {slot.slot_id}",
                    actual="; ".join(problems) or None,
                )
            )
        return results


def assert_no_viz_producer(binding: BindingIR) -> CheckResult:
    """MVP profile rejects ``viz`` producers (v1.1 §4.1 / D8)."""
    viz_slots = [
        b.slot_id for b in binding.bindings if isinstance(b, BoundBinding) and b.producer == "viz"
    ]
    return CheckResult(
        check_id="producer_profile",
        rule_id=_RULE,
        status=GateStatus.FAIL if viz_slots else GateStatus.PASS,
        actual=f"viz producers: {viz_slots}" if viz_slots else None,
    )
