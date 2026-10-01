"""Application Service command / receipt DTOs (v1.1 §5).

Commands are what the outside world (CLI, later Web) sends; receipts are
idempotent, re-showable answers. Body bytes arrive only inside commands and
go straight to the artifact pool — never into the ledger.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omas.domain.common import UtcDatetime
from omas.domain.delivery import Delivery
from omas.domain.ids import AwaitingEventId, DecisionId, TaskId, TemplateVersionId
from omas.domain.task import DataPolicy, Task


class MaterialInput(BaseModel):
    """One material supplied with a command (bytes stay in the pool)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: str = Field(min_length=1, max_length=255)
    content: bytes


class SubmitTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=200)
    template_version_id: TemplateVersionId
    intent: str = Field(min_length=1)
    materials: tuple[MaterialInput, ...] = Field(min_length=1, max_length=50)
    data_policy: DataPolicy = DataPolicy.LOCAL_ONLY


class RespondTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: TaskId
    decision_id: DecisionId
    expected_epoch: int = Field(ge=1)
    awaiting_event_id: AwaitingEventId
    action: str  # "provide_material" | "omit_slot"
    slot_id: str | None = None
    materials: tuple[MaterialInput, ...] = ()


class CancelTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: TaskId
    request_id: str = Field(min_length=1)


class RecoverTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: TaskId
    request_id: str = Field(min_length=1)


class ExportTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: TaskId
    request_id: str = Field(min_length=1)
    output_path: str = Field(min_length=1)


class TaskReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: TaskId
    request_id: str
    replayed: bool
    created_at: UtcDatetime


class DecisionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: DecisionId
    task_id: TaskId
    epoch_after: int
    replayed: bool
    accepted_at: UtcDatetime


class ExportReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: TaskId
    delivery_id: str
    exported_to: str
    sha256: str
    replayed: bool


class EventItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int
    event_code: str
    refs: dict[str, str]
    counts: dict[str, int]
    created_at: UtcDatetime


class EventPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[EventItem, ...]
    last_seq: int


class AwaitingInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    awaiting_event_id: AwaitingEventId
    epoch: int
    missing_slot_ids: tuple[str, ...]


class TaskView(BaseModel):
    """Everything a status call shows; never body text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: Task
    awaiting: AwaitingInfo | None = None
    delivery: Delivery | None = None
    material_count: int = 0


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    AWAITING_USER = "awaiting_user"
    FAILED = "failed"


class ExecutionOutcome(BaseModel):
    """What an executor (deterministic driver now, LangGraph later) reports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ExecutionStatus
    delivery_id: str | None = None
    missing_slot_ids: tuple[str, ...] = ()
    error_code: str | None = None
