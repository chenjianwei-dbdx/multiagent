"""Single-writer executor lock for one OMAS_HOME (v1.1 §1.1 / §8.2).

One OMAS_HOME allows exactly one writing executor. This is a coarse mutual
exclusion between processes (CLI / future worker), not a multi-worker
scheduler: contention is an operational condition, not a throughput feature.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from omas.domain.errors import ConcurrencyError


@contextmanager
def executor_lock(omas_home: Path, timeout: float = 30.0) -> Iterator[FileLock]:
    """Hold the exclusive write lock for *omas_home* for the enclosed block."""
    lock = FileLock(str(omas_home / "runtime" / "executor.lock"), timeout=timeout)
    try:
        lock.acquire()
    except Timeout as exc:
        raise ConcurrencyError(
            "another executor holds the write lock for this OMAS_HOME"
        ) from exc
    try:
        yield lock
    finally:
        lock.release()
