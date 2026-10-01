"""Crash-window recovery via adopt_existing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omas.artifacts.store import ArtifactStore
from omas.domain.errors import (
    ArtifactHashMismatchError,
    ArtifactNotFoundError,
    PathEscapeError,
)

DATA = b"orphaned but complete\n"


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path)


def _promote_without_registering(store: ArtifactStore) -> tuple[str, str]:
    """Simulate the crash window: file promoted, ledger registration never ran."""
    staged = store.stage("task_1", "run_1", "a.txt", DATA)
    relative = store.node_out_relative("task_1", "run_1", "a.txt")
    store.promote(staged, relative)
    return relative, staged.sha256


def test_adopt_existing_after_crash_window(store: ArtifactStore) -> None:
    relative, sha = _promote_without_registering(store)
    adopted = store.adopt_existing(relative, sha)
    assert adopted.relative_path == relative
    assert adopted.sha256 == sha
    assert adopted.size == len(DATA)


def test_adopt_rejects_hash_mismatch(store: ArtifactStore) -> None:
    relative, _ = _promote_without_registering(store)
    with pytest.raises(ArtifactHashMismatchError):
        store.adopt_existing(relative, "f" * 64)
    # The unverifiable file is rejected, not consumed or modified.
    assert store.read(relative) == DATA


def test_adopt_missing_file(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        store.adopt_existing("tasks/task_1/canonical/art_missing.txt", "0" * 64)


def test_adopt_rejects_symlink(store: ArtifactStore, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(DATA)
    canonical = store.root / "tasks" / "t1" / "canonical"
    canonical.mkdir(parents=True)
    os.symlink(outside, canonical / "art_x.txt")
    with pytest.raises(PathEscapeError):
        store.adopt_existing("tasks/t1/canonical/art_x.txt", "0" * 64)
