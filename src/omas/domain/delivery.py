"""Delivery: the single committed, exportable result of a task (MVP: one per task)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .common import Sha256Hex, UtcDatetime
from .ids import ArtifactId, DeliveryId, OperationId, TaskId


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: DeliveryId
    task_id: TaskId
    operation_id: OperationId
    #: the gate-passing candidate this delivery froze
    candidate_artifact_id: ArtifactId
    final_sha256: Sha256Hex
    manifest_artifact_id: ArtifactId
    created_at: UtcDatetime

    def receipt_digest(self) -> str:
        """Stable digest for idempotent receipt comparison."""
        return f"{self.task_id}:{self.final_sha256}:{self.manifest_artifact_id}"
