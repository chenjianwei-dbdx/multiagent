"""BindingService (P2 second half): the program owns BindingIR writes (I3).

The Assembler expert only *proposes* (``slot_id + status + span_handles``);
this service is the single authoritative writer that turns a proposal into a
persisted :class:`~omas.domain.ir.BindingIR` (v1.1 §4.1 / §4.2):

- the task must be non-terminal and in the epoch the proposal was made for;
- every ``span_handle`` must exist in the ledger's ``resolved_spans`` index
  (forged handles are rejected, T18), must belong to this task (cross-task
  handles are rejected) and its artifact must still be registered;
- the :class:`~omas.domain.spans.SourceSpanRef` is rebuilt **exclusively from
  the DB row** (artifact_id / canonical_sha256 / start / end / span_sha256) —
  nothing in a binding comes from model output — and is then re-verified
  against the file (hash-verified read + re-canonicalisation +
  :func:`omas.core.spans.verify_span`), so tampered material cannot be bound
  (T03);
- bound slots get ``producer='user'`` (MVP binds user-supplied material only;
  ``viz``/``upstream`` are structurally impossible because the proposal DTO
  has no producer field at all), spans keep the proposal's order;
- required contract slots the proposal never mentioned are completed with
  ``missing(no_material)`` entries so gap_check sees the full slot picture;
- the material set digest binds the IR to the task's current canonical_text
  artifacts (stale bindings cannot survive re-supply, docs/01 §4.1);
- persistence is content-addressed and replay-safe: the binding JSON lands
  under ``nodes/run_assemble/out/<sha>.json`` through
  :class:`~omas.artifacts.writers.NodeArtifactWriter` (existing identical
  files are adopted/reused, never overwritten), then one ``bindings`` row per
  slot (``binding_version`` starting at 1 within each task epoch), the task's
  active binding ref is updated and one metadata-only event is appended.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import ValidationError

from omas.agents.assembler import MISSING_REASON_CODES, AssemblerProposal
from omas.artifacts.store import ArtifactStore, StagedFile
from omas.artifacts.writers import ArtifactRecorder, NodeArtifactWriter
from omas.core.canonical import canonicalize, sha256_bytes
from omas.core.digest import canonical_model_sha
from omas.core.digest import material_set_digest as _material_set_digest
from omas.core.spans import verify_span
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.errors import (
    ArtifactExistsError,
    ArtifactHashMismatchError,
    ArtifactNotFoundError,
    DecodeError,
    OmasError,
    SpanResolutionError,
)
from omas.domain.ids import ArtifactId, SpanHandle, TaskId, TemplateVersionId, new_id
from omas.domain.ir import (
    SCHEMA_VERSION,
    BindingIR,
    BoundBinding,
    ContentPlanIR,
    InvalidBinding,
    MissingBinding,
    SlotBinding,
)
from omas.domain.spans import SourceSpanRef
from omas.domain.task import Task
from omas.domain.template import TemplateContract
from omas.pipeline.recorders import LedgerRecorder
from omas.storage.db import Ledger

__all__ = ["BindingRejectedError", "BindingService"]

#: The Literal of MissingBinding.reason_code, mirrored for a sound cast after
#: runtime membership validation (the proposal DTO carries a plain str).
_MissingReason = Literal["no_material", "ambiguous", "budget_exceeded", "policy_blocked"]


class BindingRejectedError(OmasError):
    """A binding proposal was refused before anything was persisted.

    Covers: terminal/stale task, unbound template, inconsistent plan, forged
    or cross-task span handles, handles whose material no longer verifies
    (tampered file, hash mismatch), slots outside the contract and invalid
    missing-reason mappings. Messages carry ids/codes only, never body text.
    """

    code = "BINDING_REJECTED"


#: Stable node-scoped run id for binding commits (v1.1 §5: node output lives
#: under its run directory; the path is task-scoped by the store).
_BINDING_RUN_ID = "run_assemble"

_VERIFY_ERRORS: tuple[type[Exception], ...] = (
    ArtifactNotFoundError,
    ArtifactHashMismatchError,
    DecodeError,
    SpanResolutionError,
    ValidationError,
)


class BindingService:
    """Commits Assembler proposals as authoritative BindingIRs."""

    def __init__(self, ledger: Ledger, store: ArtifactStore) -> None:
        self._ledger = ledger
        self._store = store
        self._recorder: ArtifactRecorder = LedgerRecorder(ledger)

    # ---------------------------------------------------------------- public

    def commit_proposal(
        self,
        *,
        task: Task,
        proposal: AssemblerProposal,
        plan: ContentPlanIR | None = None,
        plan_artifact_id: ArtifactId,
        contract: TemplateContract,
    ) -> tuple[BindingIR, Artifact]:
        """Verify *proposal* and persist it as this task's binding.

        ``plan`` is the (optional) :class:`~omas.domain.ir.ContentPlanIR` the
        proposal was assembled under; pass ``None`` when no plan artifact
        exists yet (P3 wires the real plans). Returns the committed
        ``(BindingIR, Artifact)`` pair; raises :class:`BindingRejectedError`
        before any write on any violation.
        """
        fresh = self._require_committable_task(task)
        self._require_plan_consistency(fresh, plan, contract)
        template_version_id = fresh.template_version_id
        if template_version_id is None:  # pragma: no cover - guarded above
            raise BindingRejectedError(f"task {task.task_id} has no bound template version")

        material_pairs = self._material_pairs(TaskId(fresh.task_id))
        bindings = self._materialize_bindings(fresh, proposal, contract)
        # Required-but-unmentioned slots become explicit missing entries so
        # gap_check sees the complete slot picture (docs/01 §8).
        present = {binding.slot_id for binding in bindings}
        for spec in contract.slots:
            if spec.required and spec.slot_id not in present:
                bindings.append(
                    MissingBinding(
                        binding_status="missing",
                        slot_id=spec.slot_id,
                        reason_code="no_material",
                    )
                )

        task_id = TaskId(fresh.task_id)
        ir = BindingIR(
            schema_version=SCHEMA_VERSION,
            task_id=task_id,
            plan_artifact_id=plan_artifact_id,
            template_version_id=TemplateVersionId(template_version_id),
            epoch=fresh.epoch,
            binding_version=self._next_binding_version(task_id, fresh.epoch),
            material_set_digest=_material_set_digest(material_pairs),
            bindings=tuple(bindings),
        )

        artifact = self._persist_binding(fresh.task_id, ir, plan_artifact_id)
        self._insert_binding_rows(ir)
        self._ledger.tasks.set_active_refs(task_id, binding=ArtifactId(artifact.artifact_id))
        self._ledger.events.append(
            task_id,
            "binding_committed",
            refs={
                "node": "assemble_bind",
                "binding_artifact_id": artifact.artifact_id,
                "plan_artifact_id": plan_artifact_id,
            },
            counts={
                "epoch": fresh.epoch,
                "binding_version": ir.binding_version,
                "slots": len(ir.bindings),
                "bound": sum(
                    1 for b in ir.bindings if b.binding_status == "bound"
                ),
                "missing": sum(
                    1 for b in ir.bindings if b.binding_status == "missing"
                ),
                "invalid": sum(
                    1 for b in ir.bindings if b.binding_status == "invalid"
                ),
            },
        )
        return ir, artifact

    # -------------------------------------------------------------- internals

    def _require_committable_task(self, task: Task) -> Task:
        """Re-read the task from the ledger and enforce commit preconditions."""
        fresh = self._ledger.tasks.get(task.task_id)
        if fresh is None:
            raise BindingRejectedError(f"task {task.task_id} not found")
        if fresh.is_terminal():
            raise BindingRejectedError(
                f"task {task.task_id} is in terminal status {fresh.status.value}"
            )
        if fresh.epoch != task.epoch:
            raise BindingRejectedError(
                f"stale proposal: task {task.task_id} is at epoch {fresh.epoch}, "
                f"proposal was made for epoch {task.epoch}"
            )
        if fresh.template_version_id is None:
            raise BindingRejectedError(f"task {task.task_id} has no bound template version")
        return fresh

    def _require_plan_consistency(
        self, task: Task, plan: ContentPlanIR | None, contract: TemplateContract
    ) -> None:
        """Plan (when present) must describe this task's template and slots."""
        if plan is None:
            return
        if plan.task_id != task.task_id:
            raise BindingRejectedError(
                f"plan task {plan.task_id} does not match binding task {task.task_id}"
            )
        if plan.template_version_id != task.template_version_id:
            raise BindingRejectedError(
                "plan template version does not match the task's bound version"
            )
        unknown = set(plan.slot_ids()) - set(contract.slot_ids())
        if unknown:
            raise BindingRejectedError(
                f"plan references slots outside the contract: {sorted(unknown)}"
            )

    def _material_pairs(self, task_id: TaskId) -> list[tuple[str, str]]:
        """(artifact_id, sha256) of every canonical_text material of the task."""
        rows = self._ledger.connection.execute(
            "SELECT artifact_id, sha256 FROM artifacts"
            " WHERE task_id = ? AND kind = ? ORDER BY artifact_id",
            (task_id, ArtifactKind.CANONICAL_TEXT.value),
        ).fetchall()
        return [(str(row["artifact_id"]), str(row["sha256"])) for row in rows]

    def _materialize_bindings(
        self, task: Task, proposal: AssemblerProposal, contract: TemplateContract
    ) -> list[SlotBinding]:
        """Turn proposals into the discriminated union, verifying every handle."""
        contract_slots = set(contract.slot_ids())
        bindings: list[SlotBinding] = []
        for slot in proposal.slots:
            if slot.slot_id not in contract_slots:
                raise BindingRejectedError(
                    f"proposal names slot outside the contract: {slot.slot_id}"
                )
            if slot.binding_status == "bound":
                # producer is program-decided (MVP: user materials only); span
                # order is the proposal's order, each ref re-verified below.
                bindings.append(
                    BoundBinding(
                        binding_status="bound",
                        slot_id=slot.slot_id,
                        producer="user",
                        source_refs=tuple(
                            self._verified_ref(TaskId(task.task_id), handle)
                            for handle in slot.span_handles
                        ),
                    )
                )
            elif slot.binding_status == "missing":
                reason = slot.reason_code
                if reason not in MISSING_REASON_CODES:
                    raise BindingRejectedError(
                        f"missing slot {slot.slot_id} carries an invalid reason "
                        f"mapping: {reason!r}"
                    )
                bindings.append(
                    MissingBinding(
                        binding_status="missing",
                        slot_id=slot.slot_id,
                        reason_code=cast(_MissingReason, reason),
                    )
                )
            else:
                if not slot.error_code:
                    raise BindingRejectedError(
                        f"invalid slot {slot.slot_id} carries no error_code"
                    )
                bindings.append(
                    InvalidBinding(
                        binding_status="invalid",
                        slot_id=slot.slot_id,
                        error_code=slot.error_code,
                    )
                )
        return bindings

    def _verified_ref(self, task_id: TaskId, handle: str) -> SourceSpanRef:
        """Resolve one span handle to a re-verified SourceSpanRef.

        The ref is rebuilt from the ``resolved_spans`` row alone; the file is
        then re-read, re-canonicalised and the span re-verified, so neither a
        forged handle nor tampered bytes can survive (T03/T18).
        """
        row = self._ledger.resolved_spans.get(SpanHandle(handle))
        if row is None:
            raise BindingRejectedError(
                f"span handle was never issued by a tool call: {handle}"
            )
        if str(row["task_id"]) != task_id:
            raise BindingRejectedError(
                f"span handle {handle} belongs to another task (rejected)"
            )
        artifact = self._ledger.artifacts.get(ArtifactId(str(row["artifact_id"])))
        if artifact is None or artifact.task_id != task_id:
            raise BindingRejectedError(
                f"span handle {handle} references material outside this task"
            )
        try:
            ref = SourceSpanRef(
                artifact_id=ArtifactId(str(row["artifact_id"])),
                canonical_sha256=str(row["canonical_sha256"]),
                start=int(row["start"]),
                end=int(row["end"]),
                span_sha256=str(row["span_sha256"]),
            )
            data = self._store.read_verified(artifact.relative_path, artifact.sha256)
            canonical = canonicalize(data)
            if canonical.sha256 != artifact.sha256:
                raise BindingRejectedError(
                    f"material {artifact.artifact_id} is no longer canonical text"
                )
            verify_span(canonical, ref)
        except _VERIFY_ERRORS as exc:
            raise BindingRejectedError(
                f"span handle {handle} no longer verifies against its material "
                f"({type(exc).__name__})"
            ) from exc
        return ref

    def _next_binding_version(self, task_id: TaskId, epoch: int) -> int:
        """binding_version starts at 1 within each (task, epoch)."""
        row = self._ledger.connection.execute(
            "SELECT COALESCE(MAX(binding_version), 0) AS v FROM bindings"
            " WHERE task_id = ? AND epoch = ?",
            (task_id, epoch),
        ).fetchone()
        return int(row["v"]) + 1

    def _insert_binding_rows(self, ir: BindingIR) -> None:
        """One ledger row per slot (refs only; spans stored as ref JSON)."""
        created_at = datetime.now(UTC)
        for binding in ir.bindings:
            if binding.binding_status == "bound":
                producer = binding.producer
                source_refs = list(binding.source_refs)
            else:
                producer = None
                source_refs = []
            self._ledger.bindings.insert(
                binding_id=new_id("bind"),
                task_id=ir.task_id,
                epoch=ir.epoch,
                binding_version=ir.binding_version,
                slot_id=binding.slot_id,
                binding_status=binding.binding_status,
                producer=producer,
                source_refs=source_refs,
                created_at=created_at,
            )

    def _persist_binding(
        self, task_id: str, ir: BindingIR, plan_artifact_id: ArtifactId
    ) -> Artifact:
        """Content-addressed ``nodes/run_assemble/out/<sha>.json``; replays
        reuse or adopt the existing file instead of duplicating it."""
        name = f"{canonical_model_sha(ir)}.json"
        data = ir.model_dump_json().encode("utf-8")
        out_relative = self._store.node_out_relative(task_id, _BINDING_RUN_ID, name)
        if self._store.exists(out_relative):
            registered = self._ledger.artifacts.get_by_path(out_relative)
            if registered is not None:
                return registered
            promoted = self._store.adopt_existing(out_relative, sha256_bytes(data))
            return self._ledger.artifacts.register(
                Artifact(
                    artifact_id=ArtifactId(new_id("art")),
                    task_id=TaskId(task_id),
                    kind=ArtifactKind.BINDING_IR,
                    relative_path=promoted.relative_path,
                    sha256=promoted.sha256,
                    size=promoted.size,
                    source_refs=(plan_artifact_id,),
                    content_type="application/json",
                    created_at=datetime.now(UTC),
                )
            )
        writer = NodeArtifactWriter(
            self._store, task_id, _BINDING_RUN_ID, recorder=self._recorder
        )
        try:
            staged = writer.stage(name, data)
        except ArtifactExistsError:
            staged = self._readopt_staged(task_id, name, sha256_bytes(data))
        return writer.commit_out(
            staged,
            kind=ArtifactKind.BINDING_IR,
            final_name=name,
            source_refs=(plan_artifact_id,),
            content_type="application/json",
        )

    def _readopt_staged(
        self, task_id: str, name: str, expected_sha: str
    ) -> StagedFile:
        """Re-verify and re-adopt a work file left behind by an earlier attempt."""
        work_relative = self._store.node_work_relative(task_id, _BINDING_RUN_ID, name)
        self._store.verify(work_relative, expected_sha)
        return StagedFile(
            path=self._store.resolve_path(work_relative),
            relative_path=work_relative,
            name=name,
            sha256=expected_sha,
            size=len(self._store.read(work_relative)),
        )
