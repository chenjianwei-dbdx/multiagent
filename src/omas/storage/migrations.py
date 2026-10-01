"""Versioned SQL migration runner (stdlib sqlite3 only; no ORM/Alembic).

Each ``NNN_name.sql`` file under ``migrations/`` is applied in ascending
version order, one transaction per version, and recorded in
``schema_migrations`` together with a sha256 checksum of the script text.

Already applied versions are checksum-verified and skipped; any drift
(different checksum, missing file for a recorded version, gap or out-of-order
version numbers) is a hard :class:`MigrationError`. Migrations never drop or
recreate existing tables at startup.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omas.domain.errors import MigrationError

from .db import to_db_datetime

MIGRATIONS_DIR: Path = Path(__file__).resolve().parent / "migrations"

_FILENAME_RE = re.compile(r"^(\d{3,})_([A-Za-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class MigrationFile:
    """One discovered migration script with its computed checksum."""

    version: int
    name: str
    path: Path
    text: str
    checksum: str


def discover_migrations(migrations_dir: Path) -> list[MigrationFile]:
    """List migration scripts in version order, rejecting gaps and duplicates.

    Version numbers must form the contiguous sequence ``1..N``; a gap, a
    duplicate number or a non-``NNN_name.sql`` filename is a MigrationError.
    """
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")
    found: list[MigrationFile] = []
    seen: set[int] = set()
    for path in migrations_dir.glob("*.sql"):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        version = int(match.group(1))
        name = match.group(2)
        if version in seen:
            raise MigrationError(f"duplicate migration version: {version}")
        seen.add(version)
        text = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        found.append(MigrationFile(version, name, path, text, checksum))
    found.sort(key=lambda migration: migration.version)
    if [migration.version for migration in found] != list(range(1, len(found) + 1)):
        raise MigrationError(
            "migration versions must start at 1 and be contiguous without gaps, got: "
            + ", ".join(str(migration.version) for migration in found)
        )
    return found


def _applied_checksums(conn: sqlite3.Connection) -> dict[int, str]:
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if has_table is None:
        return {}
    rows = conn.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {int(row["version"]): str(row["checksum"]) for row in rows}


def _apply(conn: sqlite3.Connection, migration: MigrationFile) -> None:
    """Apply one migration in a single transaction including its bookkeeping row.

    The DDL and the ``schema_migrations`` insert are committed atomically, so a
    crash mid-migration leaves no half-applied version behind.
    """
    applied_at = to_db_datetime(datetime.now(UTC))
    # Values below are safe to interpolate: name matches [A-Za-z0-9_]+,
    # checksum is hex, applied_at is produced by to_db_datetime.
    bookkeeping = (
        "INSERT INTO schema_migrations (version, name, checksum, applied_at)\n"
        f"VALUES ({migration.version}, '{migration.name}', "
        f"'{migration.checksum}', '{applied_at}');"
    )
    script = f"BEGIN IMMEDIATE;\n{migration.text}\n{bookkeeping}\nCOMMIT;"
    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        conn.rollback()
        raise MigrationError(
            f"migration {migration.version:03d}_{migration.name} failed: {exc}"
        ) from exc


def run_migrations(
    conn: sqlite3.Connection, migrations_dir: Path | None = None
) -> list[int]:
    """Bring the database up to the latest version.

    Returns the list of newly applied versions (empty when already current).
    Raises :class:`MigrationError` on checksum drift, missing recorded files,
    non-prefix applied state, or gaps in the file sequence.
    """
    directory = migrations_dir if migrations_dir is not None else MIGRATIONS_DIR
    files = discover_migrations(directory)
    applied = _applied_checksums(conn)
    applied_versions = sorted(applied)

    file_by_version = {migration.version: migration for migration in files}
    for version in applied_versions:
        if version not in file_by_version:
            raise MigrationError(
                f"version {version} is recorded in schema_migrations but has no "
                "migration file"
            )
    expected_prefix = [migration.version for migration in files[: len(applied_versions)]]
    if applied_versions != expected_prefix:
        raise MigrationError(
            f"applied versions {applied_versions} are not a contiguous prefix of "
            f"available migrations {expected_prefix}"
        )
    for version in applied_versions:
        migration = file_by_version[version]
        if applied[version] != migration.checksum:
            raise MigrationError(
                f"checksum mismatch for migration {version:03d}_{migration.name}: "
                f"recorded {applied[version]}, file {migration.checksum}"
            )

    newly_applied: list[int] = []
    for migration in files:
        if migration.version in applied:
            continue
        _apply(conn, migration)
        newly_applied.append(migration.version)
    return newly_applied
