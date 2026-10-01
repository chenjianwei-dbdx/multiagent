"""Production glue between the artifact pool and the ledger (I3 seam)."""

from __future__ import annotations

from omas.domain.artifact import Artifact
from omas.storage.db import Ledger


class LedgerRecorder:
    """ArtifactRecorder backed by the SQLite ledger.

    The only bridge through which promoted files become registered business
    artifacts; every writer (node / delivery) receives this, never the ledger
    itself.
    """

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def register(self, artifact: Artifact) -> Artifact:
        return self._ledger.artifacts.register(artifact)
