"""Shared P1 closed-loop fixtures and builders."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import InboxWriter
from omas.core import CanonicalText, resolve_span
from omas.core.digest import material_set_digest, payload_digest
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.ids import ArtifactId, TaskId, new_task_id
from omas.domain.task import DataPolicy, Task
from omas.pipeline.recorders import LedgerRecorder
from omas.storage.db import Ledger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures.weekly_report import build_weekly_report_template


@dataclass
class LoopEnv:
    home: Path
    ledger: Ledger
    store: ArtifactStore
    recorder: LedgerRecorder


@pytest.fixture()
def loop_env(tmp_path: Path) -> LoopEnv:
    home = tmp_path / "home"
    home.mkdir()
    ledger = Ledger.open(home / "ledger.sqlite3")
    store = ArtifactStore(home)
    yield LoopEnv(home=home, ledger=ledger, store=store, recorder=LedgerRecorder(ledger))
    ledger.close()


@pytest.fixture(scope="session")
def weekly():
    return build_weekly_report_template()


def make_task(env: LoopEnv, request_id: str = "req-p1") -> Task:
    now = datetime.now(UTC)
    return env.ledger.tasks.create(
        Task(
            task_id=new_task_id(),
            request_id=request_id,
            data_policy=DataPolicy.LOCAL_ONLY,
            created_at=now,
            updated_at=now,
        ),
        payload_digest("p1"),
    )


def ingest(
    env: LoopEnv, task_id: str, text: str, name: str = "week.md"
) -> tuple[Artifact, CanonicalText]:
    _raw, cfile, canonical = InboxWriter(env.store).ingest_text(
        task_id, name, text.encode("utf-8")
    )
    artifact = Artifact(
        artifact_id=ArtifactId(cfile.name.removesuffix(".txt")),
        task_id=TaskId(task_id),
        kind=ArtifactKind.CANONICAL_TEXT,
        relative_path=cfile.relative_path,
        sha256=canonical.sha256,
        size=canonical.size_bytes,
        source_refs=(),
        created_at=datetime.now(UTC),
    )
    return env.ledger.artifacts.register(artifact), canonical


def sections_of(material_markdown: str) -> dict[str, list[str]]:
    """Split the fixture material into body paragraphs per ## section."""
    lines = material_markdown.splitlines()
    sections: dict[str, list[str]] = {}
    order = ["sales_summary", "risks", "next_plan"]
    current: str | None = None
    buffer: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current is not None:
                sections[current] = [p for p in buffer if p]
            current = order[len(sections)] if len(sections) < len(order) else None
            buffer = []
        elif current is not None:
            buffer.append(line.strip())
    if current is not None:
        sections[current] = [p for p in buffer if p]
    return sections


def binding_for_material(
    env: LoopEnv,
    task: Task,
    version_id: str,
    material: Artifact,
    canonical: CanonicalText,
    spans_by_slot: dict[str, list[str]],
    extra_bindings: list | None = None,
) -> tuple:
    """Build spans + BindingIR + persisted binding artifact for the fixture slots."""
    from omas.domain.ir import BindingIR, BoundBinding

    if extra_bindings is None:
        extra_bindings = []
    bindings: list = []
    for slot_id, texts in spans_by_slot.items():
        refs = []
        for text in texts:
            start = canonical.text.index(text)
            refs.append(
                resolve_span(material.artifact_id, canonical, start, start + len(text))
            )
        bindings.append(
            BoundBinding(
                binding_status="bound",
                slot_id=slot_id,
                producer="user",
                source_refs=tuple(refs),
            )
        )
    bindings.extend(extra_bindings)
    digest = material_set_digest([(material.artifact_id, material.sha256)])
    binding = BindingIR(
        schema_version=1,
        task_id=TaskId(task.task_id),
        plan_artifact_id=ArtifactId(material.artifact_id),
        template_version_id=version_id,  # type: ignore[arg-type]
        epoch=task.epoch,
        binding_version=1,
        material_set_digest=digest,
        bindings=bindings,
    )
    binding_bytes = binding.model_dump_json().encode("utf-8")
    from omas.artifacts.writers import NodeArtifactWriter
    from omas.core.canonical import sha256_bytes

    writer = NodeArtifactWriter(env.store, task.task_id, "run_binding", recorder=env.recorder)
    staged = writer.stage("binding.json", binding_bytes)
    artifact = writer.commit_out(
        staged,
        kind=ArtifactKind.BINDING_IR,
        final_name=f"{sha256_bytes(binding_bytes)}.json",
        content_type="application/json",
    )
    return binding, artifact


def styles_spec_typed(raw: dict) -> dict:
    from omas.domain.template import StyleSpec

    return {
        key: StyleSpec.model_validate({**value, "style_key": key})
        for key, value in raw.items()
    }
