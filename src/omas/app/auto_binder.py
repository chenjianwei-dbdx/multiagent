"""Deterministic fallback binder: markdown sections → slots, no LLM.

Used when no model is configured (current deployment state): slots bind to
the material section carrying the matching heading; sections that appear in
no material stay ``missing`` and surface through gap_check like any other
gap. The real path (Planner + Assembler + BindingService) replaces this the
moment a model configuration exists (ADR D19).
"""

from __future__ import annotations

from datetime import UTC, datetime

from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import NodeArtifactWriter
from omas.core import canonicalize, resolve_span
from omas.core.canonical import sha256_bytes
from omas.core.digest import material_set_digest
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.ids import ArtifactId, TaskId, TemplateVersionId
from omas.domain.ir import BindingIR, BoundBinding, MissingBinding
from omas.domain.task import Task
from omas.domain.template import TemplateContract
from omas.pipeline.recorders import LedgerRecorder
from omas.storage.db import Ledger

_HEADING_KEYS = {
    "销售": "sales",
    "风险": "risk",
    "计划": "plan",
}

_SECTION_MARKERS = {
    "sales_summary": ("本周销售", "销售情况", "sales"),
    "risks": ("风险", "依赖", "risk"),
    "next_plan": ("下周计划", "计划", "plan"),
}


def _marker_for(slot_id: str, semantic: str) -> str | None:
    keys = _SECTION_MARKERS.get(slot_id)
    if keys is None:
        return None
    for key in keys:
        if key in semantic:
            return key
    return None


class AutoBinder:
    """Program-side binder used in offline (no-model) mode."""

    def __init__(self, ledger: Ledger, store: ArtifactStore) -> None:
        self._ledger = ledger
        self._store = store

    def bind(self, task: Task, contract: TemplateContract) -> tuple[BindingIR, Artifact]:
        committed = self._committed_binding(task)
        if committed is not None:
            return committed
        rows = self._ledger.connection.execute(
            "SELECT artifact_id FROM artifacts WHERE task_id = ? AND kind = 'canonical_text'"
            " ORDER BY created_at",
            (task.task_id,),
        ).fetchall()
        materials = []
        for row in rows:
            artifact = self._ledger.artifacts.get(ArtifactId(row["artifact_id"]))
            if artifact is not None:
                materials.append(artifact)
        canonicals = [
            (m, canonicalize(self._store.read_verified(m.relative_path, m.sha256)))
            for m in materials
        ]
        bindings: list[BoundBinding | MissingBinding] = []
        for spec in contract.slots:
            marker = _marker_for(spec.slot_id, spec.semantic_requirement)
            bound = None
            if marker is not None:
                for material, canonical in canonicals:
                    lines = canonical.text.splitlines()
                    hit = next(
                        (
                            i
                            for i, line in enumerate(lines)
                            if line.startswith("#") and marker in line
                        ),
                        None,
                    )
                    if hit is None:
                        continue
                    start = sum(len(line) + 1 for line in lines[:hit]) + len(lines[hit]) + 1
                    end = canonical.code_points
                    for i in range(hit + 1, len(lines)):
                        if lines[i].startswith("#"):
                            end = sum(len(line) + 1 for line in lines[:i])
                            break
                    if start < end:
                        bound = resolve_span(material.artifact_id, canonical, start, end)
                        break
            if bound is None:
                bindings.append(
                    MissingBinding(
                        binding_status="missing", slot_id=spec.slot_id, reason_code="no_material"
                    )
                )
            else:
                bindings.append(
                    BoundBinding(
                        binding_status="bound", slot_id=spec.slot_id, producer="user",
                        source_refs=(bound,),
                    )
                )
        binding = BindingIR(
            schema_version=1,
            task_id=TaskId(task.task_id),
            plan_artifact_id=(
                ArtifactId(materials[0].artifact_id)
                if materials
                else ArtifactId("art_" + "0" * 32)
            ),
            template_version_id=task.template_version_id
            or TemplateVersionId("tver_" + "0" * 32),
            epoch=task.epoch,
            binding_version=1,
            material_set_digest=material_set_digest((m.artifact_id, m.sha256) for m in materials),
            bindings=tuple(bindings),
        )
        payload = binding.model_dump_json().encode("utf-8")
        self._record_binding_rows(task, bindings)
        writer = NodeArtifactWriter(
            self._store, task.task_id, "run_assemble", recorder=LedgerRecorder(self._ledger)
        )
        staged = writer.stage("binding.json", payload)
        artifact = writer.commit_out(
            staged,
            kind=ArtifactKind.BINDING_IR,
            final_name=f"{sha256_bytes(payload)}.json",
            content_type="application/json",
        )
        return binding, artifact

    def _committed_binding(self, task: Task) -> tuple[BindingIR, Artifact] | None:
        """Idempotent rebind within one epoch: reuse the committed artifact.

        The production graph invokes the binder from both assemble_bind and
        render_and_finalize within one run; the artifact pool is immutable, so
        re-committing identical content must reuse the promoted file instead of
        re-promoting (ArtifactExistsError) — same contract as the test
        SectionBinder (v1.1 §8.1).
        """
        existing = self._ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM bindings WHERE task_id = ? AND epoch = ?",
            (task.task_id, task.epoch),
        ).fetchone()
        if existing is None or existing["n"] == 0:
            return None
        rows = self._ledger.connection.execute(
            "SELECT relative_path FROM artifacts WHERE task_id = ?"
            " AND kind = 'binding_ir' ORDER BY created_at DESC",
            (task.task_id,),
        ).fetchall()
        for row in rows:
            artifact = self._ledger.artifacts.get_by_path(row["relative_path"])
            if artifact is None:
                continue
            binding = BindingIR.model_validate_json(
                self._store.read_verified(artifact.relative_path, artifact.sha256)
            )
            if binding.epoch == task.epoch:
                return binding, artifact
        return None

    def _record_binding_rows(
        self, task: Task, bindings: list[BoundBinding | MissingBinding]
    ) -> None:
        existing = self._ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM bindings WHERE task_id = ? AND epoch = ?",
            (task.task_id, task.epoch),
        ).fetchone()
        if existing is not None and existing["n"] > 0:
            return
        for index, slot_binding in enumerate(bindings):
            self._ledger.bindings.insert(
                binding_id=f"bnd_{task.task_id.removeprefix('task_')}_{task.epoch}_{index}",
                task_id=TaskId(task.task_id),
                epoch=task.epoch,
                binding_version=1,
                slot_id=slot_binding.slot_id,
                binding_status=slot_binding.binding_status,
                producer=getattr(slot_binding, "producer", None),
                source_refs=getattr(slot_binding, "source_refs", ()),
                created_at=datetime.now(UTC),
            )
