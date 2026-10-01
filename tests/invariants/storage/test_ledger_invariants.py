"""Ledger invariants (I4 etc.): no body bytes in SQLite, corrupt marking, uniques."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.decisions import AwaitingEvent
from omas.domain.errors import ArtifactNotFoundError
from omas.domain.ids import (
    AwaitingEventId,
    TaskId,
    new_artifact_id,
    new_awaiting_event_id,
    new_operation_id,
    new_task_id,
)
from omas.domain.operations import Operation, OperationState
from omas.domain.task import DataPolicy, Task, TaskStatus
from omas.storage import Ledger

DIGEST_A = "a" * 64
TS = "2026-01-01T00:00:00.000000Z"


@pytest.fixture()
def ledger(tmp_path):  # type: ignore[no-untyped-def]
    led = Ledger.open(tmp_path / "ledger.db")
    yield led
    led.close()


# ------------------------------------------------- no body text in SQLite


def test_no_body_text_in_sqlite(tmp_path: Path) -> None:
    """The artifact's bytes (with a unique canary) must never reach the ledger.

    SQLite is refs-and-metadata only (invariant I4): the canary must be absent
    from the checkpointed main database file and from the WAL file bytes.
    """
    db_path = tmp_path / "ledger.db"
    led = Ledger.open(db_path)
    try:
        canary = f"CANARY-{uuid.uuid4().hex}"
        now = datetime.now(UTC)

        task = Task(
            task_id=new_task_id(),
            request_id="req-canary",
            status=TaskStatus.CREATED,
            data_policy=DataPolicy.LOCAL_ONLY,
            epoch=1,
            created_at=now,
            updated_at=now,
        )
        led.tasks.create(task, DIGEST_A)

        operation = Operation(
            operation_id=new_operation_id(),
            operation_key=f"opkey-{new_operation_id()}",
            state=OperationState.INTENT,
            task_id=task.task_id,
            node_name="ingest",
            epoch=1,
            payload_digest=DIGEST_A,
            created_at=now,
        )
        led.operations.ensure_intent(operation)

        body = (canary + "\n").encode("utf-8") * 64
        relative_path = f"tasks/{task.task_id}/intent.txt"
        pool_file = tmp_path / "pool" / relative_path
        pool_file.parent.mkdir(parents=True)
        pool_file.write_bytes(body)

        artifact = Artifact(
            artifact_id=new_artifact_id(),
            task_id=task.task_id,
            kind=ArtifactKind.INTENT,
            relative_path=relative_path,
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            created_by_operation_id=operation.operation_id,
            source_refs=(),
            created_at=now,
        )
        stored = led.artifacts.register(artifact)
        led.events.append(
            task.task_id,
            "artifact.registered",
            {"artifact_id": stored.artifact_id, "path": relative_path},
            {"bytes": len(body)},
        )

        # flush the WAL into the main file before inspecting raw bytes
        led.checkpoint()

        db_bytes = db_path.read_bytes()
        wal_path = db_path.parent / (db_path.name + "-wal")
        wal_bytes = wal_path.read_bytes() if wal_path.exists() else b""

        # positive controls: the refs/metadata really are in the database and
        # the canary really is in the pool file
        assert stored.artifact_id.encode() in db_bytes
        assert relative_path.encode() in db_bytes
        assert canary.encode() in pool_file.read_bytes()

        # the invariant: no body text anywhere in the ledger bytes
        assert canary.encode() not in db_bytes
        assert canary.encode() not in wal_bytes
    finally:
        led.close()


# ------------------------------------------------------------ corrupt marking


def test_mark_corrupt_is_idempotent_and_keeps_first_time(ledger: Ledger) -> None:
    now = datetime.now(UTC)
    artifact = Artifact(
        artifact_id=new_artifact_id(),
        task_id=None,
        kind=ArtifactKind.RAW_TEXT,
        relative_path="pool/orphan-raw.txt",
        sha256="c" * 64,
        size=10,
        created_at=now,
    )
    registered = ledger.artifacts.register(artifact)

    first_at = datetime(2026, 1, 1, tzinfo=UTC)
    second_at = first_at + timedelta(days=7)

    first = ledger.artifacts.mark_corrupt(registered.artifact_id, first_at)
    second = ledger.artifacts.mark_corrupt(registered.artifact_id, second_at)

    assert first == first_at
    assert second == first_at  # replay keeps the first recorded time

    row = ledger.connection.execute(
        "SELECT corrupt_at FROM artifacts WHERE artifact_id = ?",
        (registered.artifact_id,),
    ).fetchone()
    assert row["corrupt_at"] == "2026-01-01T00:00:00.000000Z"

    with pytest.raises(ArtifactNotFoundError):
        ledger.artifacts.mark_corrupt(new_artifact_id(), first_at)


# --------------------------------------------------------- unique constraints


def test_operations_operation_key_unique(ledger: Ledger) -> None:
    conn = ledger.connection
    insert = (
        "INSERT INTO operations (operation_id, operation_key, state, payload_digest,"
        " created_at) VALUES (?, 'shared-key', 'intent', ?, ?)"
    )
    conn.execute(insert, (new_operation_id(), DIGEST_A, TS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (new_operation_id(), DIGEST_A, TS))


def test_artifacts_relative_path_unique(ledger: Ledger) -> None:
    conn = ledger.connection
    insert = (
        "INSERT INTO artifacts (artifact_id, kind, relative_path, sha256, size,"
        " created_at) VALUES (?, 'raw_text', 'pool/same-path.txt', ?, 1, ?)"
    )
    conn.execute(insert, (new_artifact_id(), DIGEST_A, TS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (new_artifact_id(), DIGEST_A, TS))


def test_user_decisions_decision_id_unique(ledger: Ledger) -> None:
    now = datetime.now(UTC)
    task = Task(
        task_id=new_task_id(),
        request_id="req-unique-decision",
        status=TaskStatus.CREATED,
        data_policy=DataPolicy.LOCAL_ONLY,
        epoch=1,
        created_at=now,
        updated_at=now,
    )
    ledger.tasks.create(task, DIGEST_A)
    awaiting_event_id = new_awaiting_event_id()
    ledger.awaiting_events.create(
        AwaitingEvent(
            awaiting_event_id=AwaitingEventId(awaiting_event_id),
            task_id=TaskId(task.task_id),
            epoch=1,
            missing_slot_ids=("slot_a",),
            created_at=now,
        )
    )
    conn = ledger.connection
    insert = (
        "INSERT INTO user_decisions (decision_id, task_id, expected_epoch,"
        " awaiting_event_id, action, payload_digest, created_at)"
        " VALUES (?, ?, 1, ?, 'provide_material', ?, ?)"
    )
    # the same decision_id cannot be inserted twice
    decision_id = "dec_fixed00000000000000000000000001"
    conn.execute(insert, (decision_id, task.task_id, awaiting_event_id, DIGEST_A, TS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (decision_id, task.task_id, awaiting_event_id, DIGEST_A, TS))


def test_concurrent_write_transactions_are_serialised(tmp_path):
    """Real models emit several tool calls per response; concurrent
    ``BEGIN IMMEDIATE`` on the shared connection must not explode."""
    import threading

    from omas.storage.db import Ledger

    home = tmp_path / "home"
    home.mkdir()
    ledger = Ledger.open(home / "ledger.sqlite3")
    try:
        now_task = __import__("datetime").datetime.now(__import__("datetime").UTC)
        from omas.domain.ids import new_task_id
        from omas.domain.task import DataPolicy, Task

        task = ledger.tasks.create(
            Task(
                task_id=new_task_id(),
                request_id="req-conc",
                data_policy=DataPolicy.LOCAL_ONLY,
                created_at=now_task,
                updated_at=now_task,
            ),
            "e" * 64,
        )
        errors: list[Exception] = []

        def worker(index: int) -> None:
            try:
                for _ in range(10):
                    ledger.events.append(
                        task.task_id, f"concurrent_{index}", refs={}, counts={}
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        count = ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE task_id = ?", (task.task_id,)
        ).fetchone()["n"]
        assert count == 40
    finally:
        ledger.close()


def test_reader_writer_race_yields_clean_rows(tmp_path):
    """Bare SELECTs on the shared connection must not race committed INSERTs.

    Live incident: a tool thread reading the artifacts table while another
    tool thread committed an insert got a corrupted cursor — a NOT NULL
    ``size`` column read back as None (TypeError in _row_to_meta).
    """
    import threading
    from datetime import UTC, datetime

    from omas.domain.artifact import Artifact, ArtifactKind
    from omas.domain.ids import TaskId, new_artifact_id, new_task_id
    from omas.domain.task import DataPolicy, Task
    from omas.storage.db import Ledger, reading

    home = tmp_path / "home"
    home.mkdir()
    ledger = Ledger.open(home / "ledger.sqlite3")
    try:
        now = datetime.now(UTC)
        task = ledger.tasks.create(
            Task(
                task_id=new_task_id(),
                request_id="req-race",
                data_policy=DataPolicy.LOCAL_ONLY,
                created_at=now,
                updated_at=now,
            ),
            "e" * 64,
        )
        errors: list[Exception] = []
        rounds = 60

        def writer() -> None:
            try:
                for i in range(rounds):
                    ledger.artifacts.register(
                        Artifact(
                            artifact_id=new_artifact_id(),
                            task_id=TaskId(task.task_id),
                            kind=ArtifactKind.CANONICAL_TEXT,
                            relative_path=f"tasks/{task.task_id}/canonical/w{i}.txt",
                            sha256=f"{i:064x}",
                            size=i + 1,
                            source_refs=(),
                            created_at=datetime.now(UTC),
                        )
                    )
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                conn = ledger.connection
                for _ in range(rounds):
                    with reading(conn):
                        rows = conn.execute(
                            "SELECT artifact_id, task_id, kind, relative_path, sha256,"
                            " size FROM artifacts WHERE task_id = ?",
                            (task.task_id,),
                        ).fetchall()
                    for row in rows:
                        # the corrupted-cursor symptom: NOT NULL size read as None
                        assert row["size"] is not None
                        int(row["size"])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
    finally:
        ledger.close()
