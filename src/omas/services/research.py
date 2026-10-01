"""Program-level material writer for the research node (ADR D27).

The research node lets an LLM decide *which* web pages are relevant; the
bytes themselves are fetched and canonicalised **here, by the program**.
The model never supplies content — it supplies a URL. That keeps I2 exactly
as written: body text still only comes from template static content or
precise spans of registered materials; a research material is simply a
material the program registered on the task's behalf, with its network
origin recorded in ``research_sources`` for traceability.

Hard borders honoured:

- hash / span / task scope are generated and verified by the program
  (InboxWriter → canonical text → Artifact row; ids are ``new_*``);
- registration is content-addressed and idempotent per (task, URL):
  re-running the research node on resume returns the existing artifact
  instead of refetching — no duplicate materials, no IDEMPOTENCY_CONFLICT
  for program writes (that protocol scopes *user* operations, cf. seeding);
- every fetch runs through ``ModelGateway`` as a ``web_fetch`` before any
  transport is constructed (``local_only`` never reaches here — the node
  gates on policy — but the guard is defense in depth).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import InboxWriter
from omas.core.canonical import canonicalize
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.errors import OmasError
from omas.domain.ids import ArtifactId, TaskId, new_artifact_id, new_operation_id
from omas.storage.db import Ledger

#: Cap one research material at this many canonical characters; the model
#: gets bounded context and the assembler gets bounded spans to choose from.
RESEARCH_MAX_CHARS: int = 12000

#: Hard cap on sources registered per task (budget + anti-runaway).
RESEARCH_MAX_SOURCES: int = 12

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class ResearchMaterial:
    """Outcome of one ``add_source`` call: what got registered (or reused)."""

    artifact: Artifact
    url: str
    registered: bool  # False ⇒ (task, url) already had a material


class ResearchService:
    """Registers web-fetched text as task materials with provenance.

    Constructed per task scope (like :class:`omas.tools.materials.MaterialToolkit`
    for the read side); the agent's ``add_source`` tool is a thin wrapper over
    :meth:`register_fetched`.
    """

    def __init__(self, store: ArtifactStore, ledger: Ledger, task_id: str) -> None:
        self._store = store
        self._ledger = ledger
        self._task_id = task_id

    # ------------------------------------------------------------------ sink

    def source_count(self) -> int:
        return len(self._ledger.research_sources.for_task(self._task_id))

    def register_fetched(
        self, *, url: str, query: str, text: str, http_status: int = 200
    ) -> ResearchMaterial:
        """Register ``text`` (already fetched by the program) as a material.

        Idempotent per (task, url): returns the existing material when the
        URL already has one. Raises when the budget is exhausted or the
        canonical text is unusable.
        """
        existing = self._existing(url)
        if existing is not None:
            return ResearchMaterial(artifact=existing, url=url, registered=False)
        if self.source_count() >= RESEARCH_MAX_SOURCES:
            raise OmasError(
                f"research budget exhausted: at most {RESEARCH_MAX_SOURCES} sources per task"
            )
        body = text[:RESEARCH_MAX_CHARS]
        canonical = canonicalize(body.encode("utf-8"))
        if not canonical.text.strip():
            raise OmasError("research page text is empty after canonicalisation")
        artifact = self._ingest(_filename_for(url, self.source_count()), canonical.text)
        row = self._ledger.research_sources.register(
            source_id=f"rsrc_{new_operation_id()[-24:]}",
            task_id=self._task_id,
            artifact_id=artifact.artifact_id,
            source_url=url,
            query=query[:400],
            http_status=http_status,
            fetched_at=datetime.now(UTC),
        )
        if row is None:
            raise OmasError(f"research source registration vanished for {url}")
        return ResearchMaterial(artifact=artifact, url=url, registered=True)

    def register_text(self, *, label: str, query: str, text: str) -> ResearchMaterial:
        """Register program-side synthesised text (not URL-backed).

        Used for the research digest the agent composes from the pages it
        registered: a material like any other, carrying the query as origin
        (``program://``). It is a *derivative* of this very step, so it does
        not consume the web-source budget (``RESEARCH_MAX_SOURCES``); the web
        cap exists to bound fetching, not synthesis.
        """
        body = text[: RESEARCH_MAX_CHARS * 2]
        canonical_body = canonicalize(body.encode("utf-8")).text
        if not canonical_body.strip():
            raise OmasError("research text is empty after canonicalisation")
        url = f"program://{label}"
        existing = self._existing(url)
        if existing is not None:
            return ResearchMaterial(artifact=existing, url=url, registered=False)
        artifact = self._ingest(
            _filename_for(url, self.source_count()), canonical_body
        )
        row = self._ledger.research_sources.register(
            source_id=f"rsrc_{new_operation_id()[-24:]}",
            task_id=self._task_id,
            artifact_id=artifact.artifact_id,
            source_url=url,
            query=query[:400],
            http_status=0,
            fetched_at=datetime.now(UTC),
        )
        if row is None:
            raise OmasError(f"research text registration vanished for {label}")
        return ResearchMaterial(artifact=artifact, url=f"program://{label}", registered=True)

    # ------------------------------------------------------------------ read

    def list_sources(self) -> list[dict[str, str]]:
        rows = self._ledger.research_sources.for_task(self._task_id)
        return [
            {
                "url": str(row["source_url"]),
                "query": str(row["query"]),
                "artifact_id": str(row["artifact_id"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------ internals

    def _existing(self, url: str) -> Artifact | None:
        row = self._ledger.connection.execute(
            "SELECT artifact_id FROM research_sources WHERE task_id = ? AND source_url = ?",
            (self._task_id, url),
        ).fetchone()
        if row is None:
            return None
        return self._ledger.artifacts.get(ArtifactId(row["artifact_id"]))

    def _ingest(self, filename: str, text: str) -> Artifact:
        _raw, cfile, canonical = InboxWriter(self._store).ingest_text(
            self._task_id, filename, text.encode("utf-8")
        )
        artifact = Artifact(
            artifact_id=new_artifact_id(),
            task_id=TaskId(self._task_id),
            kind=ArtifactKind.CANONICAL_TEXT,
            relative_path=cfile.relative_path,
            sha256=canonical.sha256,
            size=canonical.size_bytes,
            source_refs=(),
            created_at=datetime.now(UTC),
        )
        return self._ledger.artifacts.register(artifact)


def _filename_for(url: str, index: int) -> str:
    """Deterministic inbox filename for one source (URL-agnostic index)."""
    slug = _UNSAFE_NAME.sub("-", url)[:40].strip("-") or "page"
    return f"research_{index:03d}_{slug}.txt"
