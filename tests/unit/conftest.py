"""Shared fixtures for module-level tests that need a real ledger + pool."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import InboxWriter
from omas.core import CanonicalText
from omas.core.digest import payload_digest
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.ids import (
    ArtifactId,
    TaskId,
    TemplateVersionId,
    new_task_id,
    new_template_version_id,
)
from omas.domain.task import DataPolicy, Task
from omas.pipeline.recorders import LedgerRecorder
from omas.storage.db import Ledger

H64 = "a" * 64


@dataclass
class EnvBox:
    home: Path
    ledger: Ledger
    store: ArtifactStore
    recorder: LedgerRecorder


@pytest.fixture()
def env(tmp_path: Path) -> EnvBox:
    home = tmp_path / "home"
    home.mkdir()
    ledger = Ledger.open(home / "ledger.sqlite3")
    store = ArtifactStore(home)
    yield EnvBox(home=home, ledger=ledger, store=store, recorder=LedgerRecorder(ledger))
    ledger.close()


def make_task(env: EnvBox, request_id: str = "req-1", payload: str = "p") -> Task:
    now = datetime.now(UTC)
    task = Task(
        task_id=new_task_id(),
        request_id=request_id,
        data_policy=DataPolicy.LOCAL_ONLY,
        created_at=now,
        updated_at=now,
    )
    return env.ledger.tasks.create(task, payload_digest(payload))


def ingest_material(
    env: EnvBox, task_id: str, text: str, name: str = "m.md"
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


def register_template(env: EnvBox, template_id: str = "weekly-report") -> TemplateVersionId:
    """Register a template version whose hashes match a buildable contract.

    Idempotent per ledger: a second call returns the already-registered row.
    """
    existing = env.ledger.connection.execute(
        "SELECT template_version_id FROM template_versions WHERE template_id = ?"
        " ORDER BY version LIMIT 1",
        (template_id,),
    ).fetchone()
    if existing is not None:
        return TemplateVersionId(existing["template_version_id"])
    version_id = new_template_version_id()
    env.ledger.template_versions.register(
        template_version_id=version_id,
        template_id=template_id,
        version=1,
        docx_sha256=H64,
        contract_sha256="b" * 64,
        styles_sha256="c" * 64,
        static_map_sha256="d" * 64,
        extractor_version="test-1",
        created_at=datetime.now(UTC),
    )
    return version_id
