"""Shared fixtures for graph tests: fake collaborators + render kwargs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import pytest

from fixtures.weekly_report import build_weekly_report_template
from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import NodeArtifactWriter
from omas.core import resolve_span
from omas.core.canonical import sha256_bytes
from omas.core.digest import material_set_digest
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.ids import ArtifactId, TaskId
from omas.domain.ir import BindingIR, BoundBinding, MissingBinding
from omas.domain.task import Task
from omas.domain.template import TemplateContract
from omas.graph.deps import GraphDeps, RenderKwargs
from omas.pipeline.recorders import LedgerRecorder
from omas.storage.db import Ledger


class SectionBinder:
    """Deterministic fake collaborator: binds section text spans, gaps stay missing."""

    def __init__(self, ledger: Ledger, store: ArtifactStore, drop: set[str]) -> None:
        self._ledger = ledger
        self._store = store
        self._drop = drop

    def bind(self, task: Task, contract: TemplateContract) -> tuple[BindingIR, Artifact]:
        # idempotent rebind within one epoch: reuse the committed artifact

        existing_rows = self._ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM bindings WHERE task_id = ? AND epoch = ?",
            (task.task_id, task.epoch),
        ).fetchone()
        if existing_rows is not None and existing_rows["n"] > 0:
            rows = self._ledger.connection.execute(
                "SELECT relative_path FROM artifacts WHERE task_id = ?"
                " AND kind = 'binding_ir' ORDER BY created_at DESC LIMIT 1",
                (task.task_id,),
            ).fetchall()
            if rows:
                artifact = self._ledger.artifacts.get_by_path(rows[0]["relative_path"])
                if artifact is not None:
                    binding = BindingIR.model_validate_json(
                        self._store.read_verified(artifact.relative_path, artifact.sha256)
                    )
                    return binding, artifact
        rows = self._ledger.connection.execute(
            "SELECT artifact_id FROM artifacts WHERE task_id = ? AND kind = 'canonical_text'",
            (task.task_id,),
        ).fetchall()
        materials = []
        for row in rows:
            artifact = self._ledger.artifacts.get(ArtifactId(row["artifact_id"]))
            if artifact is not None:
                materials.append(artifact)
        from omas.core import canonicalize

        canonicals = [
            (m, canonicalize(self._store.read_verified(m.relative_path, m.sha256)))
            for m in materials
        ]
        headings = {
            "sales_summary": "## 一、本周销售情况",
            "risks": "## 二、风险与依赖",
            "next_plan": "## 三、下周计划",
        }
        bindings: list = []
        for slot_id, marker in headings.items():
            if slot_id in self._drop:
                bindings.append(
                    MissingBinding(
                        binding_status="missing", slot_id=slot_id, reason_code="no_material"
                    )
                )
                continue
            found = None
            for material, canonical in canonicals:
                at = canonical.text.find(marker)
                if at >= 0:
                    nexts = [
                        canonical.text.find(m)
                        for m in headings.values()
                        if canonical.text.find(m) > at
                    ]
                    end = min(nexts) if nexts else canonical.code_points
                    found = (
                        material,
                        resolve_span(material.artifact_id, canonical, at + len(marker), end),
                    )
                    break
            if found is None:
                bindings.append(
                    MissingBinding(
                        binding_status="missing", slot_id=slot_id, reason_code="no_material"
                    )
                )
                continue
            bindings.append(
                BoundBinding(
                    binding_status="bound", slot_id=slot_id, producer="user",
                    source_refs=(found[1],),
                )
            )
        binding = BindingIR(
            schema_version=1,
            task_id=TaskId(task.task_id),
            plan_artifact_id=ArtifactId(materials[0].artifact_id),
            template_version_id=task.template_version_id or "",
            epoch=task.epoch,
            binding_version=1,
            material_set_digest=material_set_digest(
                (m.artifact_id, m.sha256) for m in materials
            ),
            bindings=bindings,
        )
        from datetime import UTC, datetime

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
        payload = binding.model_dump_json().encode("utf-8")
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


@pytest.fixture(scope="session")
def weekly():
    return build_weekly_report_template()


class GraphEnv:
    def __init__(self, home: Path, ledger: Ledger, store: ArtifactStore, weekly) -> None:
        self.home = home
        self.ledger = ledger
        self.store = store
        self.weekly = weekly
        self.version_id = None
        self.contract = None

    def setup_template(self) -> None:
        from omas.templates.registry import TemplateRegistry

        registry = TemplateRegistry(self.store, self.ledger)
        existing = self.ledger.connection.execute(
            "SELECT template_version_id FROM template_versions WHERE template_id = ?"
            " ORDER BY version LIMIT 1",
            ("weekly-report",),
        ).fetchone()
        if existing is not None:
            from omas.domain.ids import TemplateVersionId

            version_id = TemplateVersionId(existing["template_version_id"])
            self.version_id = version_id
            self.contract = registry.get_contract(version_id)
            return
        version_id, contract = registry.register(
            docx_bytes=self.weekly.docx_bytes,
            sidecar=self.weekly.sidecar,
            styles_spec=self.weekly.styles_spec,
            static_map=self.weekly.static_map,
            template_id="weekly-report",
        )
        assert contract.is_activatable()
        self.version_id = version_id
        self.contract = contract

    def render_kwargs(self, task_id: str) -> RenderKwargs | None:
        from omas.domain.template import StyleSpec

        return RenderKwargs(
            contract=self.contract,
            template_version_id=self.version_id,
            template_docx=self.weekly.docx_bytes,
            template_relative_path=self.store.template_relative(
                "weekly-report", "1", "template.docx"
            ),
            static_map=self.weekly.static_map,
            styles_spec={
                key: StyleSpec.model_validate({**value, "style_key": key})
                for key, value in self.weekly.styles_spec.items()
            },
        )

    def deps(self, drop: set[str]) -> GraphDeps:
        return GraphDeps(
            home=self.home,
            store=self.store,
            ledger=self.ledger,
            planner=None,
            binder=SectionBinder(self.ledger, self.store, drop),
        )


@pytest.fixture()
def graph_env(tmp_path: Path, weekly) -> GraphEnv:
    home = tmp_path / "home"
    home.mkdir()
    ledger = Ledger.open(home / "ledger.sqlite3")
    store = ArtifactStore(home)
    env = GraphEnv(home, ledger, store, weekly)
    env.setup_template()
    yield env
    ledger.close()
