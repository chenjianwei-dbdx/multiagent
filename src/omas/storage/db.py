"""SQLite connection factory, PRAGMA baseline and the :class:`Ledger` facade.

The ledger is refs-and-metadata only (invariant I4): body text and file bytes
live in the artifact pool on the filesystem, never in this database.

Concurrency model: a file-level single-writer lock exists outside this module
(AGENTS.md), so connections are opened with ``check_same_thread=False`` and
every repository method runs in one short explicit ``BEGIN IMMEDIATE``
transaction. No LLM call or render ever waits inside a transaction.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final

BUSY_TIMEOUT_MS: Final[int] = 5000


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a ledger connection with the mandated PRAGMA baseline.

    - ``row_factory = sqlite3.Row`` (dict-like rows)
    - ``foreign_keys = ON`` (executed per connection; SQLite default is OFF)
    - ``busy_timeout = 5000`` ms, ``journal_mode = WAL``, ``synchronous = FULL``
    - autocommit mode (``isolation_level=None``): transactions are started
      explicitly via :func:`immediate`, never implicitly
    """
    conn = sqlite3.connect(
        str(db_path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def utc_now() -> datetime:
    """Current timezone-aware UTC time (ledger write timestamps)."""
    return datetime.now(UTC)


def to_db_datetime(value: datetime) -> str:
    """Serialise an aware datetime as UTC ISO-8601 with an explicit ``Z``."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def from_db_datetime(value: str) -> datetime:
    """Parse a stored ISO-8601 timestamp back into an aware UTC datetime."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


_LOCKS: dict[int, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(conn: sqlite3.Connection) -> threading.RLock:
    """Per-connection reentrant mutex.

    Real models can return several tool calls in ONE response; pydantic-ai
    executes sync tools in worker threads, so ALL access to the shared
    connection must be serialised. Observed live, in two rounds:

    1. concurrent ``BEGIN IMMEDIATE`` → "cannot start a transaction within a
       transaction" (write/write);
    2. a bare SELECT racing a committed INSERT → corrupted cursor data, a
       NOT NULL column read back as ``None`` (read/write).

    Reentrant so a same-thread read inside a write transaction cannot
    self-deadlock; cross-thread mutual exclusion is unchanged.
    """
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(id(conn), threading.RLock())


@contextmanager
def reading(conn: sqlite3.Connection) -> Iterator[None]:
    """Serialise a bare read (or read sequence) on the shared connection.

    Autocommit snapshot reads; no BEGIN is issued. Tool code running in
    agent worker threads must wrap its raw ``conn.execute`` reads in this.
    """
    with _lock_for(conn):
        yield


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """One explicit short write transaction.

    Takes the write lock up front (``BEGIN IMMEDIATE``), commits on success,
    rolls back on any exception. Never nest.
    """
    with _lock_for(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


class Ledger:
    """Owns one connection, applies migrations and exposes repositories."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        # Deferred imports: db.py is imported by migrations.py and
        # repositories.py for the helpers above, so importing them at module
        # level here would create a cycle.
        from . import repositories as _repositories
        from .migrations import run_migrations as _run_migrations

        self.connection = connection
        _run_migrations(connection)
        self.tasks = _repositories.TaskRepository(connection)
        self.operations = _repositories.OperationRepository(connection)
        self.artifacts = _repositories.ArtifactRepository(connection)
        self.events = _repositories.EventRepository(connection)
        self.node_runs = _repositories.NodeRunRepository(connection)
        self.template_versions = _repositories.TemplateVersionRepository(connection)
        self.awaiting_events = _repositories.AwaitingEventRepository(connection)
        self.decisions = _repositories.DecisionRepository(connection)
        self.deliveries = _repositories.DeliveryRepository(connection)
        self.bindings = _repositories.BindingRepository(connection)
        self.slot_overrides = _repositories.SlotOverrideRepository(connection)
        self.gate_reports = _repositories.GateReportRepository(connection)
        self.llm_calls = _repositories.LlmCallRepository(connection)
        self.resolved_spans = _repositories.ResolvedSpanRepository(connection)
        self.conversations = _repositories.ConversationRepository(connection)
        self.conversation_messages = _repositories.ConversationMessageRepository(connection)
        self.template_meta = _repositories.TemplateMetaRepository(connection)
        self.research_sources = _repositories.ResearchSourceRepository(connection)

    @classmethod
    def open(cls, db_path: Path) -> Ledger:
        """Connect (creating parent directories) and apply pending migrations."""
        parent = db_path.parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        return cls(connect(db_path))

    def close(self) -> None:
        self.connection.close()

    def checkpoint(self) -> None:
        """Flush and truncate the WAL into the main database file."""
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
