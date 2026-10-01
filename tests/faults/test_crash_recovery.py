"""T05/T21: interrupt survives process death; resume in a NEW process
delivers exactly once (offline, deterministic collaborators)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_DRIVER = Path(__file__).resolve().parent / "driver.py"


def _run(phase: str, home: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(_DRIVER), phase, str(home), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip().splitlines()[-1]


def test_interrupt_survives_process_death_and_resumes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    task_id = _run("phase1", home)
    assert task_id.startswith("task_")
    delivery_id = _run("phase2", home, task_id)
    assert delivery_id.startswith("dlv_")

    # verify from a third process perspective: exactly one delivery, task completed
    from omas.domain.ids import TaskId
    from omas.storage.db import Ledger

    ledger = Ledger.open(home / "ledger.sqlite3")
    try:
        row = ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE task_id = ?", (task_id,)
        ).fetchone()
        assert row["n"] == 1
        task = ledger.tasks.get(TaskId(task_id))
        assert task is not None and task.status.value == "completed"
        docx = home / "tasks" / task_id / "deliverables" / f"{task_id}.docx"
        assert docx.exists()
    finally:
        ledger.close()


def test_duplicate_phase2_run_yields_single_delivery(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    task_id = _run("phase1", home)
    first = _run("phase2", home, task_id)
    second = _run("phase2", home, task_id)  # replay: decision already committed
    assert first == second
    from omas.storage.db import Ledger

    ledger = Ledger.open(home / "ledger.sqlite3")
    try:
        row = ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE task_id = ?", (task_id,)
        ).fetchone()
        assert row["n"] == 1
    finally:
        ledger.close()
