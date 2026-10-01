"""Capability-separated writers over the artifact pool (I3).

Every shared object has exactly one authoritative writer (v1.1 §5):

- :class:`NodeArtifactWriter` — the only capability handed to a node run; it
  can address nothing but its own task's ``nodes/<run_id>/work`` and ``out``
  directories. There is deliberately **no** API that takes another task id,
  run id or an absolute path — scope is bound at construction.
- :class:`DeliveryWriter` — held only by the Finalizer (P1); writes nothing
  but its task's ``deliverables/``.
- :class:`InboxWriter` — used by ingest; writes raw bytes to ``inbox/`` and
  canonical text to ``canonical/``.

Artifact registration is injected through :class:`ArtifactRecorder` so this
module stays independent of the SQLite ledger (storage layer, P0 sibling).
With ``recorder=None`` registration is skipped (test convenience) but the
returned :class:`~omas.domain.artifact.Artifact` DTO is always complete.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from omas.core import CanonicalText, canonicalize, find_illegal_control_chars
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.errors import CapabilityError
from omas.domain.ids import ArtifactId, OperationId, TaskId, new_artifact_id

from .store import ArtifactStore, PromotedFile, StagedFile


class ArtifactRecorder(Protocol):
    """Persistence seam implemented by the ledger (idempotent per id)."""

    def register(self, artifact: Artifact) -> Artifact:
        """Persist *artifact*; re-registering the same id returns the stored record."""
        ...


def _require_final_name(value: str, what: str) -> None:
    """A writer's output name must be a single path component."""
    if not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise CapabilityError(f"{what} must be a single path component, got {value!r}")


def _register(
    promoted: PromotedFile,
    *,
    kind: ArtifactKind,
    task_id: str,
    operation_id: OperationId | None,
    source_refs: Sequence[ArtifactId],
    content_type: str | None,
    recorder: ArtifactRecorder | None,
) -> Artifact:
    """Build the complete Artifact DTO for *promoted* and record it."""
    artifact = Artifact(
        artifact_id=new_artifact_id(),
        task_id=TaskId(task_id),
        kind=kind,
        relative_path=promoted.relative_path,
        sha256=promoted.sha256,
        size=promoted.size,
        created_by_operation_id=operation_id,
        source_refs=tuple(source_refs),
        content_type=content_type,
        created_at=datetime.now(UTC),
    )
    if recorder is not None:
        artifact = recorder.register(artifact)
    return artifact


class NodeArtifactWriter:
    """A node run's sole write capability: its own ``work/`` and ``out/``.

    Cannot address other tasks or other runs — no such parameter exists on
    any public method, and ``commit_out`` verifies the staged file actually
    lives in this run's ``work/`` directory before promoting it.
    """

    def __init__(
        self,
        store: ArtifactStore,
        task_id: str,
        run_id: str,
        recorder: ArtifactRecorder | None = None,
    ) -> None:
        self._store = store
        self._task_id = task_id
        self._run_id = run_id
        self._recorder = recorder

    def stage(self, name: str, data: bytes) -> StagedFile:
        """Stage bytes into this run's ``work/`` (hash computed on write)."""
        return self._store.stage(self._task_id, self._run_id, name, data)

    def commit_out(
        self,
        staged: StagedFile,
        kind: ArtifactKind,
        final_name: str,
        operation_id: OperationId | None = None,
        source_refs: Sequence[ArtifactId] = (),
        content_type: str | None = None,
    ) -> Artifact:
        """Promote *staged* to this run's ``out/<final_name>`` and register it."""
        _require_final_name(final_name, "final_name")
        expected_work = self._store.node_work_relative(self._task_id, self._run_id, staged.name)
        if staged.relative_path != expected_work:
            raise CapabilityError(
                f"staged file does not belong to this run's work directory: {staged.relative_path}"
            )
        final_relative = self._store.node_out_relative(self._task_id, self._run_id, final_name)
        promoted = self._store.promote(staged, final_relative)
        return _register(
            promoted,
            kind=kind,
            task_id=self._task_id,
            operation_id=operation_id,
            source_refs=source_refs,
            content_type=content_type,
            recorder=self._recorder,
        )


class DeliveryWriter:
    """The Finalizer's write capability: this task's ``deliverables/`` only."""

    def __init__(
        self,
        store: ArtifactStore,
        task_id: str,
        recorder: ArtifactRecorder | None = None,
    ) -> None:
        self._store = store
        self._task_id = task_id
        self._recorder = recorder

    def deliver(
        self,
        staged: StagedFile,
        final_name: str,
        kind: ArtifactKind = ArtifactKind.DELIVERY_DOCX,
        operation_id: OperationId | None = None,
        source_refs: Sequence[ArtifactId] = (),
        content_type: str | None = None,
    ) -> Artifact:
        """Promote *staged* to ``deliverables/<final_name>`` and register it."""
        _require_final_name(final_name, "final_name")
        final_relative = self._store.delivery_relative(self._task_id, final_name)
        promoted = self._store.promote(staged, final_relative)
        return _register(
            promoted,
            kind=kind,
            task_id=self._task_id,
            operation_id=operation_id,
            source_refs=source_refs,
            content_type=content_type,
            recorder=self._recorder,
        )


class InboxWriter:
    """Ingest capability: raw bytes to ``inbox/``, canonical text to ``canonical/``.

    Holds no task scope of its own — ``ingest_text`` takes the task id and the
    ingest service owns task scoping. Step 1 writes the raw bytes immutably
    (no overwrite; retained for provenance), step 2 writes the canonical text
    (NFC + LF, UTF-8 strict — :func:`omas.core.canonicalize`) to
    ``canonical/<artifact_id>.txt`` where the id is program-generated and
    recoverable from ``StagedFile.name``.

    Invalid UTF-8 propagates ``DecodeError`` unchanged. Forbidden control
    characters are **not** filtered: the canonical file is written as-is and
    the offending code-point positions are reported on
    ``StagedFile.illegal_positions`` for the caller to reject (v1.1 §3.2).
    """

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    def ingest_text(
        self, task_id: str, filename: str, raw: bytes
    ) -> tuple[StagedFile, StagedFile, CanonicalText]:
        """Ingest raw bytes; returns ``(raw_file, canonical_file, canonical)``."""
        canonical = canonicalize(raw)  # DecodeError propagates unchanged
        illegal = find_illegal_control_chars(canonical.text)

        raw_relative = self._store.inbox_relative(task_id, filename)
        raw_promoted = self._store.write_immutable(raw_relative, raw)

        artifact_id = new_artifact_id()
        canonical_relative = self._store.canonical_relative(task_id, artifact_id)
        canonical_bytes = canonical.text.encode("utf-8")
        canonical_promoted = self._store.write_immutable(canonical_relative, canonical_bytes)

        raw_staged = StagedFile(
            path=self._store.resolve_path(raw_relative),
            relative_path=raw_promoted.relative_path,
            name=raw_relative.rsplit("/", 1)[1],
            sha256=raw_promoted.sha256,
            size=raw_promoted.size,
        )
        canonical_staged = StagedFile(
            path=self._store.resolve_path(canonical_relative),
            relative_path=canonical_promoted.relative_path,
            name=f"{artifact_id}.txt",
            sha256=canonical.sha256,
            size=len(canonical_bytes),
            illegal_positions=illegal,
        )
        return raw_staged, canonical_staged, canonical
