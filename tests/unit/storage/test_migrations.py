"""Migration runner tests: idempotency, checksum drift, schema completeness."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from omas.domain.errors import MigrationError
from omas.storage import Ledger, connect
from omas.storage.migrations import MIGRATIONS_DIR, run_migrations

EXPECTED_TABLES = frozenset(
    {
        "schema_migrations",
        "tasks",
        "template_versions",
        "operations",
        "artifacts",
        "node_runs",
        "bindings",
        "events",
        "awaiting_events",
        "user_decisions",
        "slot_overrides",
        "gate_reports",
        "llm_calls",
        "deliveries",
        "resolved_spans",
        "conversations",
        "conversation_messages",
        "template_meta",
        "research_sources",
    }
)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _migration_rows(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    rows = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [(int(r["version"]), str(r["name"]), str(r["checksum"])) for r in rows]


def test_fresh_database_creates_all_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ledger.db")
    try:
        applied = run_migrations(conn)
        assert applied == [1, 2, 3, 4]
        assert _table_names(conn) == EXPECTED_TABLES
        rows = _migration_rows(conn)
        assert rows == [
            (
                1,
                "init",
                hashlib.sha256(
                    (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8").encode(
                        "utf-8"
                    )
                ).hexdigest(),
            ),
            (
                2,
                "web_console",
                hashlib.sha256(
                    (MIGRATIONS_DIR / "002_web_console.sql").read_text(encoding="utf-8").encode(
                        "utf-8"
                    )
                ).hexdigest(),
            ),
            (
                3,
                "qa_turns",
                hashlib.sha256(
                    (MIGRATIONS_DIR / "003_qa_turns.sql").read_text(encoding="utf-8").encode(
                        "utf-8"
                    )
                ).hexdigest(),
            ),
            (
                4,
                "research_sources",
                hashlib.sha256(
                    (MIGRATIONS_DIR / "004_research_sources.sql").read_text(
                        encoding="utf-8"
                    ).encode("utf-8")
                ).hexdigest(),
            ),
        ]
    finally:
        conn.close()


def test_connection_pragma_baseline(tmp_path: Path) -> None:
    conn = connect(tmp_path / "pragmas.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        # row_factory=sqlite3.Row: rows are dict-like
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
    finally:
        conn.close()


def test_apply_twice_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "twice.db")
    try:
        assert run_migrations(conn) == [1, 2, 3, 4]
        assert run_migrations(conn) == []
        rows = _migration_rows(conn)
        assert [row[0] for row in rows] == [1, 2, 3, 4]
        assert _table_names(conn) == EXPECTED_TABLES
    finally:
        conn.close()


def test_reopen_database_skips_applied_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "reopen.db"
    ledger = Ledger.open(db_path)
    ledger.close()
    # A second process/connection sees the same applied version and no-ops.
    conn = connect(db_path)
    try:
        assert run_migrations(conn) == []
        assert [row[0] for row in _migration_rows(conn)] == [1, 2, 3, 4]
    finally:
        conn.close()


def test_tampered_checksum_raises_and_keeps_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "tampered.db")
    try:
        run_migrations(conn)
        conn.execute("UPDATE schema_migrations SET checksum = ?", ("0" * 64,))
        with pytest.raises(MigrationError):
            run_migrations(conn)
        # no silent drop/recreate: the schema is untouched
        assert _table_names(conn) == EXPECTED_TABLES
    finally:
        conn.close()


FAKE_001 = (
    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
    " checksum TEXT NOT NULL, applied_at TEXT NOT NULL);\n"
    "CREATE TABLE t1 (x INTEGER);\n"
)
FAKE_002 = "CREATE TABLE t2 (x INTEGER);\n"


def test_missing_file_for_recorded_version_raises(tmp_path: Path) -> None:
    dir_a = tmp_path / "migs-a"
    dir_a.mkdir()
    (dir_a / "001_one.sql").write_text(FAKE_001, encoding="utf-8")
    (dir_a / "002_two.sql").write_text(FAKE_002, encoding="utf-8")
    conn = connect(tmp_path / "missing-file.db")
    try:
        assert run_migrations(conn, dir_a) == [1, 2]
        # version 2 is recorded, but the available files no longer contain it
        dir_b = tmp_path / "migs-b"
        dir_b.mkdir()
        (dir_b / "001_one.sql").write_text(FAKE_001, encoding="utf-8")
        with pytest.raises(MigrationError):
            run_migrations(conn, dir_b)
        # nothing was dropped or recreated
        assert {"schema_migrations", "t1", "t2"} <= _table_names(conn)
    finally:
        conn.close()


def test_gap_in_versions_raises(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migs-gap"
    migrations_dir.mkdir()
    (migrations_dir / "001_init.sql").write_text(
        "CREATE TABLE t1 (x INTEGER);\n", encoding="utf-8"
    )
    (migrations_dir / "003_gap.sql").write_text(
        "CREATE TABLE t3 (x INTEGER);\n", encoding="utf-8"
    )
    conn = connect(tmp_path / "gap.db")
    try:
        with pytest.raises(MigrationError):
            run_migrations(conn, migrations_dir)
        # nothing was applied, not even version 1
        assert _table_names(conn) == set()
    finally:
        conn.close()


def test_failed_migration_rolls_back_atomically(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migs-bad"
    migrations_dir.mkdir()
    (migrations_dir / "001_bad.sql").write_text(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT"
        " NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL);\n"
        "CREATE TABLE boom (x INTEGER);\n"
        "THIS IS NOT SQL;\n",
        encoding="utf-8",
    )
    conn = connect(tmp_path / "bad.db")
    try:
        with pytest.raises(MigrationError):
            run_migrations(conn, migrations_dir)
        # DDL and bookkeeping rolled back together: nothing half-applied
        assert _table_names(conn) == set()
        # the rollback left no open transaction: the connection is usable
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone() is not None
    finally:
        conn.close()
