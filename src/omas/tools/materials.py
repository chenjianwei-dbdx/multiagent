"""Assembler material tools (P2 first half, v1.1 §5 / §6).

Four task-scoped, budgeted and audit-logged read-only tools over the task's
material pool:

- :meth:`MaterialToolkit.list_materials` — bounded metadata listing (no body)
- :meth:`MaterialToolkit.search_materials` — literal/keyword search inside the
  task's canonical files with explainable scoring; queries and snippets never
  enter SQLite and no content-bearing index is ever built (files are scanned
  per call, v1.1 §6)
- :meth:`MaterialToolkit.read_material` — exact code-point slices of material
  text, hash-verified before slicing, never summarised
- :meth:`MaterialToolkit.resolve_span` — the only issuer of span handles; all
  hashes are computed by program code, a caller/model can never assert its own

Security model:

- task scope comes exclusively from the trusted :class:`RunContext` injected
  by the host; no tool accepts a ``task_id`` parameter — one simply does not
  exist in any signature, so a caller-supplied task scope is impossible rather
  than merely ignored
- cross-task or unknown ``artifact_id`` raises :class:`ArtifactNotFoundError`
  without leaking whether the artifact exists under another task
- every call appends a ``tool_call`` ledger event carrying refs/counts
  metadata only — never body text, queries or snippets
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from omas.artifacts.store import ArtifactStore
from omas.core.canonical import CanonicalText, canonicalize, find_illegal_control_chars
from omas.core.spans import resolve_span as compute_span_ref
from omas.domain.artifact import ArtifactKind
from omas.domain.errors import (
    ArtifactCorruptError,
    ArtifactNotFoundError,
    BudgetExceededError,
    LedgerConflictError,
    OmasError,
)
from omas.domain.ids import ArtifactId, NodeRunId, TaskId, new_span_handle
from omas.domain.spans import SourceSpanRef
from omas.storage.db import Ledger, reading

# ------------------------------------------------------------------- context


@dataclass(frozen=True, slots=True)
class RunContext:
    """Trusted execution context injected by the host, never by model output.

    ``task_id`` is the authoritative task scope for every tool call; ``run_id``
    (a ``NodeRunId`` string) and ``epoch`` are recorded with issued span
    handles so bindings stay traceable to the exact attempt.
    """

    task_id: TaskId
    run_id: str | None = None
    epoch: int = 1


# -------------------------------------------------------------------- budget


@dataclass(frozen=True, slots=True)
class Budget:
    """Configurable per-turn tool budget with live counters (v1.1 §6).

    Defaults are the suggested baseline (50 list items, top_k <= 10,
    snippet <= 200 chars, single read <= 8,000 chars, <= 20 tool calls per
    turn, <= 40,000 total read chars, <= 2 schema repairs, 120 s call
    timeout). Counters live on the instance (excluded from equality/repr, so
    budgets compare by configuration); ``record_*`` raises
    :class:`BudgetExceededError` when the next use would exceed a cap, and
    ``exceeded`` is a non-mutating query used to report exhaustion state.
    Running out of budget is a stop condition — the caller must surface a
    missing/awaiting outcome, never guess or summarise to stay inside budget.
    """

    max_list_items: int = 50
    search_top_k: int = 10
    snippet_chars: int = 200
    read_chars: int = 8000
    max_tool_calls_per_turn: int = 20
    total_read_chars: int = 40000
    schema_repairs: int = 2
    call_timeout_s: int = 120

    # live counters — excluded from eq/repr on purpose
    tool_calls_used: int = field(default=0, compare=False, repr=False)
    read_chars_used: int = field(default=0, compare=False, repr=False)
    schema_repairs_used: int = field(default=0, compare=False, repr=False)

    def __post_init__(self) -> None:
        limit_names = (
            "max_list_items",
            "search_top_k",
            "snippet_chars",
            "read_chars",
            "max_tool_calls_per_turn",
            "total_read_chars",
            "schema_repairs",
            "call_timeout_s",
        )
        for name in limit_names:
            if getattr(self, name) < 1:
                raise OmasError(f"budget limit {name} must be >= 1, got {getattr(self, name)}")

    @property
    def remaining_read_chars(self) -> int:
        return max(0, self.total_read_chars - self.read_chars_used)

    def exceeded(self, code: str) -> bool:
        """Whether dimension *code* is already exhausted (no recording).

        Dimensions: ``tool_calls``, ``read_chars``, ``schema_repairs``.
        """
        used_and_limit = {
            "tool_calls": (self.tool_calls_used, self.max_tool_calls_per_turn),
            "read_chars": (self.read_chars_used, self.total_read_chars),
            "schema_repairs": (self.schema_repairs_used, self.schema_repairs),
        }
        pair = used_and_limit.get(code)
        if pair is None:
            raise OmasError(f"unknown budget dimension: {code!r}")
        used, limit = pair
        return used >= limit

    def record_tool_call(self) -> None:
        """Count one tool call; raises when the per-turn cap is reached."""
        if self.tool_calls_used >= self.max_tool_calls_per_turn:
            raise BudgetExceededError(
                f"tool call budget exhausted: {self.tool_calls_used}/"
                f"{self.max_tool_calls_per_turn} calls this turn"
            )
        object.__setattr__(self, "tool_calls_used", self.tool_calls_used + 1)

    def record_read_chars(self, count: int) -> None:
        """Count *count* returned characters against the total read budget."""
        if count < 0:
            raise OmasError(f"read char count must be >= 0, got {count}")
        if count == 0:
            return
        if self.read_chars_used + count > self.total_read_chars:
            raise BudgetExceededError(
                f"read budget exhausted: {self.read_chars_used}+{count} chars would "
                f"exceed the {self.total_read_chars} char per-turn limit"
            )
        object.__setattr__(self, "read_chars_used", self.read_chars_used + count)

    def record_schema_repair(self) -> None:
        """Count one structured-output schema repair attempt."""
        if self.schema_repairs_used >= self.schema_repairs:
            raise BudgetExceededError(
                f"schema repair budget exhausted: {self.schema_repairs_used}/"
                f"{self.schema_repairs} repairs this turn"
            )
        object.__setattr__(self, "schema_repairs_used", self.schema_repairs_used + 1)


# ---------------------------------------------------------------------- DTOs
# Field names are the tool output schema the Assembler Agent will see
# (pydantic-ai derives them from these models in the later Agent task).


class MaterialSummary(BaseModel):
    """One listed material — metadata only, never body text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    #: last path segment of the artifact's registered relative path
    display_name: str
    kind: str
    size_bytes: int = Field(ge=0)
    #: number of LF-separated lines (canonical text contains no CR)
    line_count: int = Field(ge=0)
    #: number of non-blank lines (proxy for paragraph blocks)
    paragraph_count: int = Field(ge=0)


class ListResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[MaterialSummary, ...]
    #: total matching rows before the max_list_items truncation
    total_matched: int = Field(ge=0)
    truncated: bool


class SearchHit(BaseModel):
    """One scored search hit.

    ``snippet`` is a lead, not evidence: it is a fixed-size window around the
    first match and may cut words apart. Confirm the exact content with
    read_material / resolve_span before binding anything.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    #: <= snippet_chars code points; an "…" marks each truncation point
    snippet: str
    #: total term occurrences + number of distinct terms hit (explainable)
    score: int = Field(ge=0)
    match_count: int = Field(ge=0)


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[SearchHit, ...]
    #: how many of the task's canonical materials were scanned
    scanned_artifacts: int = Field(ge=0)


class ReadResult(BaseModel):
    """Exact original text slice — never summarised, never rewritten."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    total_code_points: int = Field(ge=0)
    text: str
    #: True when more content remained beyond the single-read cap
    truncated: bool


class ResolvedSpan(BaseModel):
    """A program-issued span handle plus the verified SourceSpanRef.

    The ``span_sha256`` / ``canonical_sha256`` values inside
    ``source_span_ref`` are computed by this tool over freshly loaded,
    hash-verified canonical text. The tool signature accepts no hash
    arguments of any kind — a caller cannot forge or "confirm" one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_handle: str
    source_span_ref: SourceSpanRef
    exact_text: str
    #: code-point positions of characters forbidden in renderable text,
    #: reported as-is (never filtered; the caller decides to reject)
    illegal_positions: tuple[int, ...] = ()


# ------------------------------------------------------------ internal rows


class _ArtifactMeta(NamedTuple):
    """Read-only projection of an artifacts row (refs/metadata only)."""

    artifact_id: str
    task_id: str | None
    kind: str
    relative_path: str
    sha256: str
    size: int


def _row_to_meta(row: sqlite3.Row) -> _ArtifactMeta:
    return _ArtifactMeta(
        artifact_id=str(row["artifact_id"]),
        task_id=None if row["task_id"] is None else str(row["task_id"]),
        kind=str(row["kind"]),
        relative_path=str(row["relative_path"]),
        sha256=str(row["sha256"]),
        size=int(row["size"]),
    )


def _task_artifacts(
    conn: sqlite3.Connection, task_id: TaskId, kind: str
) -> list[_ArtifactMeta]:
    with reading(conn):
        rows = conn.execute(
            "SELECT artifact_id, task_id, kind, relative_path, sha256, size FROM artifacts"
            " WHERE task_id = ? AND kind = ? ORDER BY created_at, artifact_id",
            (task_id, kind),
        ).fetchall()
    return [_row_to_meta(row) for row in rows]


def _artifact_by_id(conn: sqlite3.Connection, artifact_id: str) -> _ArtifactMeta | None:
    with reading(conn):
        row: sqlite3.Row | None = conn.execute(
            "SELECT artifact_id, task_id, kind, relative_path, sha256, size FROM artifacts"
            " WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
    return None if row is None else _row_to_meta(row)


def _make_snippet(text: str, pos: int, limit: int) -> str:
    """A <= *limit* code-point window of *text* around index *pos*.

    Truncated ends are marked with "…" (each costs one code point of the
    limit). Pure function, no filtering, no case folding.
    """
    if len(text) <= limit:
        return text
    if limit <= 3:
        return "…"
    window = limit - 2  # reserve one code point per ellipsis
    left = max(0, min(pos, len(text) - window))
    right = left + window
    return ("…" if left > 0 else "") + text[left:right] + ("…" if right < len(text) else "")


# ------------------------------------------------------------------ toolkit


class MaterialToolkit:
    """The Assembler's four read-only material tools (v1.1 §5 / §6).

    Scope: every method is hard-scoped to ``context.task_id``. There is no
    ``task_id`` parameter anywhere, so a model cannot direct a call at
    another task; artifacts registered under a different task (or to no task)
    surface as ``ArtifactNotFoundError`` with no existence leak.

    Budget: each method first consumes one tool-call unit; read results are
    additionally charged against the total read budget. All limits are
    configurable via :class:`Budget`.

    Audit: each successful call appends one ``tool_call`` event
    (refs = tool/artifact ids, counts = small integers). Body text, queries
    and snippets are never persisted here.

    The toolkit holds no write capability beyond the controlled
    ``resolved_spans`` index and the events log — it can never write
    artifacts, bindings or deliverables.
    """

    def __init__(
        self,
        ledger: Ledger,
        store: ArtifactStore,
        context: RunContext,
        budget: Budget | None = None,
    ) -> None:
        self._ledger = ledger
        self._store = store
        self._context = context
        self._budget = budget if budget is not None else Budget()

    @property
    def context(self) -> RunContext:
        return self._context

    @property
    def budget(self) -> Budget:
        return self._budget

    # ------------------------------------------------------------------ list

    def list_materials(self, kind_filter: str | None = None) -> ListResult:
        """List this task's materials with metadata only (no body text).

        Lists artifacts of one kind registered for the current task.
        ``kind_filter`` defaults to ``"canonical_text"`` (the span-addressable
        material); other registered kinds (e.g. ``"raw_text"``, ``"intent"``)
        can be listed explicitly. Returns at most ``max_list_items`` items —
        call again with a kind filter to narrow, never guess undisplayed ids.
        """
        self._budget.record_tool_call()
        kind = (
            ArtifactKind.CANONICAL_TEXT.value
            if kind_filter is None
            else self._normalize_kind(kind_filter)
        )
        metas = _task_artifacts(self._ledger.connection, self._context.task_id, kind)
        limit = self._budget.max_list_items
        items = tuple(self._summarize(meta) for meta in metas[:limit])
        result = ListResult(
            items=items,
            total_matched=len(metas),
            truncated=len(metas) > len(items),
        )
        self._audit("list_materials", counts={"items": len(items), "matched": len(metas)})
        return result

    # ---------------------------------------------------------------- search

    def search_materials(self, query: str, top_k: int = 5) -> SearchResult:
        """Literal keyword search over this task's canonical materials.

        Scoring is deliberately naive and explainable: whitespace-split terms
        are searched literally (case-sensitive; CJK needs no case) and each
        hit scores ``total term occurrences + number of distinct terms hit``.
        Results are ordered by score (descending), then artifact_id.

        IMPORTANT: each ``snippet`` is only a lead. It is a fixed-size window
        around the first match and may cut words apart — always confirm the
        exact content with read_material and bind with resolve_span.

        No index is built and nothing is persisted: canonical files are
        scanned per call and the query never enters SQLite. At most
        ``min(top_k, search_top_k)`` hits are returned.
        """
        self._budget.record_tool_call()
        if not query.strip():
            raise OmasError("query must contain at least one keyword")
        if top_k < 1:
            raise OmasError(f"top_k must be >= 1, got {top_k}")
        terms = tuple(dict.fromkeys(query.split()))
        effective_k = min(top_k, self._budget.search_top_k)
        metas = _task_artifacts(
            self._ledger.connection,
            self._context.task_id,
            ArtifactKind.CANONICAL_TEXT.value,
        )
        hits: list[SearchHit] = []
        for meta in metas:
            text = self._load_text(meta)
            occurrences = [text.count(term) for term in terms]
            total = sum(occurrences)
            if total == 0:
                continue
            distinct = sum(1 for count in occurrences if count > 0)
            first_positions = [text.find(term) for term in terms]
            first = min(pos for pos in first_positions if pos >= 0)
            hits.append(
                SearchHit(
                    artifact_id=meta.artifact_id,
                    snippet=_make_snippet(text, first, self._budget.snippet_chars),
                    score=total + distinct,
                    match_count=total,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.artifact_id))
        result = SearchResult(items=tuple(hits[:effective_k]), scanned_artifacts=len(metas))
        self._audit(
            "search_materials",
            counts={"results": len(result.items), "scanned": len(metas)},
        )
        return result

    # ------------------------------------------------------------------ read

    def read_material(
        self, artifact_id: str, start: int = 0, length: int | None = None
    ) -> ReadResult:
        """Read an exact slice of a material's original text.

        The file is hash-verified against its registered digest before
        anything is returned; offsets and lengths are Unicode code points
        (half-open ``[start, end)``). At most ``read_chars`` code points are
        returned per call (``truncated`` tells you more remained); ``length``
        omitted means "to the end". Content is returned verbatim — this tool
        never summarises, rewrites or filters. Material belonging to another
        task raises ArtifactNotFoundError.
        """
        self._budget.record_tool_call()
        meta = self._require_task_artifact(artifact_id)
        text = self._load_text(meta)
        total = len(text)
        if start < 0:
            raise OmasError(f"start must be >= 0, got {start}")
        if start > total:
            raise OmasError(f"start {start} is beyond the material's {total} code points")
        if length is not None and length < 0:
            raise OmasError(f"length must be >= 0, got {length}")
        requested = total - start if length is None else length
        end = start + min(requested, self._budget.read_chars, total - start)
        slice_ = text[start:end]
        self._budget.record_read_chars(len(slice_))
        available_end = min(start + requested, total)
        result = ReadResult(
            artifact_id=meta.artifact_id,
            start=start,
            end=end,
            total_code_points=total,
            text=slice_,
            truncated=end < available_end,
        )
        self._audit(
            "read_material",
            artifact_id=meta.artifact_id,
            counts={"chars": len(slice_), "start": start, "end": end},
        )
        return result

    # --------------------------------------------------------------- resolve

    def resolve_span(self, artifact_id: str, start: int, end: int) -> ResolvedSpan:
        """Resolve ``[start, end)`` (code points, half-open) to a span handle.

        This is the only way to obtain a bindable span. The canonical file is
        loaded, hash-verified and re-canonicalised; BOTH hashes in the
        returned ``source_span_ref`` are computed by this tool over that
        text. The signature has no hash, task or handle parameters of any
        kind — values a caller might try to pass for them are rejected by the
        signature itself (TypeError), not silently ignored.

        Idempotent: resolving the same (task, artifact, start, end) again
        returns the already-issued handle. Empty or out-of-range spans raise
        span errors; illegal control characters are reported in
        ``illegal_positions`` and otherwise returned verbatim (never
        filtered).
        """
        self._budget.record_tool_call()
        meta = self._require_task_artifact(artifact_id)
        canonical = self._load_canonical(meta)
        ref = compute_span_ref(ArtifactId(meta.artifact_id), canonical, start, end)
        exact_text = canonical.text[start:end]
        handle, reused = self._register_resolved_span(meta, ref)
        result = ResolvedSpan(
            span_handle=handle,
            source_span_ref=ref,
            exact_text=exact_text,
            illegal_positions=find_illegal_control_chars(exact_text),
        )
        self._audit(
            "resolve_span",
            artifact_id=meta.artifact_id,
            extra_refs={"span_handle": handle},
            counts={"span_chars": len(exact_text), "reused": int(reused)},
        )
        return result

    # --------------------------------------------------------------- helpers

    def _normalize_kind(self, value: str) -> str:
        if not value:
            raise OmasError("kind_filter must be a non-empty artifact kind name")
        try:
            return ArtifactKind(value).value
        except ValueError as exc:
            raise OmasError(f"unknown artifact kind: {value!r}") from exc

    def _summarize(self, meta: _ArtifactMeta) -> MaterialSummary:
        text = self._load_text(meta)
        lines = text.split("\n") if text else []
        return MaterialSummary(
            artifact_id=meta.artifact_id,
            display_name=meta.relative_path.rsplit("/", 1)[-1],
            kind=meta.kind,
            size_bytes=meta.size,
            line_count=len(lines),
            paragraph_count=sum(1 for line in lines if line.strip()),
        )

    def _load_text(self, meta: _ArtifactMeta) -> str:
        """Hash-verified raw read, decoded as strict UTF-8."""
        data = self._store.read_verified(meta.relative_path, meta.sha256)
        return data.decode("utf-8", errors="strict")

    def _load_canonical(self, meta: _ArtifactMeta) -> CanonicalText:
        """Hash-verified read plus re-canonicalisation (NFC + LF).

        Spans are only ever defined over canonical text, so the re-canonical
        digest must equal the registered digest; otherwise the file is not
        span-addressable and is refused as corrupt rather than offset-mapped.
        """
        data = self._store.read_verified(meta.relative_path, meta.sha256)
        canonical = canonicalize(data)
        if canonical.sha256 != meta.sha256:
            raise ArtifactCorruptError(
                f"artifact {meta.artifact_id} is not canonical (NFC+LF) text; "
                "spans cannot be resolved against it"
            )
        return canonical

    def _require_task_artifact(self, artifact_id: str) -> _ArtifactMeta:
        meta = _artifact_by_id(self._ledger.connection, artifact_id)
        # Uniform not-found: never reveal cross-task existence.
        if meta is None or meta.task_id != self._context.task_id:
            raise ArtifactNotFoundError(
                f"artifact {artifact_id} not found in this task's material scope"
            )
        return meta

    def _register_resolved_span(
        self, meta: _ArtifactMeta, ref: SourceSpanRef
    ) -> tuple[str, bool]:
        """Insert (or idempotently reuse) the resolved_spans row; (handle, reused)."""
        conn = self._ledger.connection
        with reading(conn):
            row: sqlite3.Row | None = conn.execute(
                "SELECT span_handle, canonical_sha256, span_sha256 FROM resolved_spans"
                " WHERE task_id = ? AND artifact_id = ? AND start = ? AND end = ?",
                (self._context.task_id, meta.artifact_id, ref.start, ref.end),
            ).fetchone()
        if row is not None:
            return self._reuse_stored_handle(row, meta, ref), True
        handle = new_span_handle()
        try:
            self._ledger.resolved_spans.insert(
                span_handle=handle,
                task_id=self._context.task_id,
                run_id=None
                if self._context.run_id is None
                else NodeRunId(self._context.run_id),
                span=ref,
                created_at=datetime.now(UTC),
            )
        except LedgerConflictError:
            # Lost a race against the UNIQUE (task, artifact, start, end):
            # re-read and reuse the stored handle.
            with reading(conn):
                fresh: sqlite3.Row | None = conn.execute(
                    "SELECT span_handle, canonical_sha256, span_sha256 FROM resolved_spans"
                    " WHERE task_id = ? AND artifact_id = ? AND start = ? AND end = ?",
                    (self._context.task_id, meta.artifact_id, ref.start, ref.end),
                ).fetchone()
            if fresh is None:
                raise
            return self._reuse_stored_handle(fresh, meta, ref), True
        return handle, False

    def _reuse_stored_handle(
        self, row: sqlite3.Row, meta: _ArtifactMeta, ref: SourceSpanRef
    ) -> str:
        stored_sha = str(row["span_sha256"])
        stored_canonical = str(row["canonical_sha256"])
        if stored_sha != ref.span_sha256 or stored_canonical != ref.canonical_sha256:
            raise ArtifactCorruptError(
                f"stored span for artifact {meta.artifact_id}"
                f" [{ref.start}, {ref.end}) no longer matches program-computed hashes"
            )
        return str(row["span_handle"])

    def _audit(
        self,
        tool: str,
        *,
        artifact_id: str | None = None,
        extra_refs: dict[str, str] | None = None,
        counts: dict[str, int],
    ) -> None:
        """Append one tool_call event — refs/counts metadata only."""
        refs = {"tool": tool}
        if artifact_id is not None:
            refs["artifact_id"] = artifact_id
        if extra_refs is not None:
            refs.update(extra_refs)
        self._ledger.events.append(
            self._context.task_id, "tool_call", refs=refs, counts=counts
        )


__all__ = [
    "Budget",
    "ListResult",
    "MaterialSummary",
    "MaterialToolkit",
    "ReadResult",
    "ResolvedSpan",
    "RunContext",
    "SearchHit",
    "SearchResult",
]
