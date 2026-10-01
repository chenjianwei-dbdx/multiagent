"""Path resolution and escape rejection for ArtifactStore."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omas.artifacts.store import ArtifactStore
from omas.domain.errors import PathEscapeError


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/etc/passwd",
        "//etc/passwd",
        "..",
        "../outside.txt",
        "a/../b.txt",
        ".",
        "./a.txt",
        "a/./b.txt",
        "a//b.txt",
        "a/",
        "a\\b.txt",
        "\\etc",
        "tasks/t1/../../escape.txt",
        "tasks/t1/inbox/\x00bad.txt",
    ],
)
def test_rejects_invalid_relative_paths(store: ArtifactStore, bad: str) -> None:
    with pytest.raises(PathEscapeError):
        store.resolve_path(bad)


def test_resolves_valid_relative_path(store: ArtifactStore, tmp_path: Path) -> None:
    resolved = store.resolve_path("tasks/task_1/inbox/a.txt")
    assert resolved == tmp_path / "tasks" / "task_1" / "inbox" / "a.txt"
    # Non-existent deep paths resolve fine (no side effects).
    assert not resolved.exists()


def test_symlinked_ancestor_rejected(store: ArtifactStore, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    task_dir = tmp_path / "tasks" / "t1"
    task_dir.mkdir(parents=True)
    os.symlink(outside, task_dir / "canonical")
    with pytest.raises(PathEscapeError):
        store.resolve_path("tasks/t1/canonical/x.txt")


def test_symlinked_final_component_rejected(store: ArtifactStore, tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    inbox = tmp_path / "tasks" / "t1" / "inbox"
    inbox.mkdir(parents=True)
    os.symlink(target, inbox / "ln.txt")
    with pytest.raises(PathEscapeError):
        store.resolve_path("tasks/t1/inbox/ln.txt")


def test_relative_helpers_generate_expected_layout(store: ArtifactStore) -> None:
    assert store.inbox_relative("task_1", "a.txt") == "tasks/task_1/inbox/a.txt"
    assert store.canonical_relative("task_1", "art_1") == "tasks/task_1/canonical/art_1.txt"
    assert store.node_work_relative("task_1", "run_1", "d.bin") == (
        "tasks/task_1/nodes/run_1/work/d.bin"
    )
    assert store.node_out_relative("task_1", "run_1", "d.bin") == (
        "tasks/task_1/nodes/run_1/out/d.bin"
    )
    assert store.delivery_relative("task_1", "final.docx") == (
        "tasks/task_1/deliverables/final.docx"
    )
    assert store.template_relative("tpl", "1", "contract.json") == (
        "templates/tpl/1/contract.json"
    )


def test_relative_helpers_reject_bad_components(store: ArtifactStore) -> None:
    with pytest.raises(PathEscapeError):
        store.inbox_relative("task/../x", "a.txt")
    with pytest.raises(PathEscapeError):
        store.node_out_relative("task_1", "run_1", "a/b.txt")
    with pytest.raises(PathEscapeError):
        store.canonical_relative("task_1", "../art")
    with pytest.raises(PathEscapeError):
        store.template_relative("tpl", "1", "..")
    with pytest.raises(PathEscapeError):
        store.delivery_relative("task_1", "")


def test_directory_helpers_create_skeleton(store: ArtifactStore, tmp_path: Path) -> None:
    assert store.inbox_dir("task_1") == tmp_path / "tasks" / "task_1" / "inbox"
    assert store.canonical_dir("task_1").is_dir()
    assert store.run_work_dir("task_1", "run_1") == (
        tmp_path / "tasks" / "task_1" / "nodes" / "run_1" / "work"
    )
    assert store.run_out_dir("task_1", "run_1").is_dir()
    assert store.run_logs_dir("task_1", "run_1").is_dir()
    assert store.projections_dir("task_1").is_dir()
    assert store.deliverables_dir("task_1").is_dir()
    assert store.template_dir("tpl", "1") == tmp_path / "templates" / "tpl" / "1"


def test_init_creates_top_level_skeleton(tmp_path: Path) -> None:
    ArtifactStore(tmp_path / "omas_home")
    for name in ("runtime", "templates", "tasks"):
        assert (tmp_path / "omas_home" / name).is_dir()
