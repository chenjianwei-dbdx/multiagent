"""Task and node-run state (Master §11; enums must not grow synonyms)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .common import Epoch, UtcDatetime
from .ids import ArtifactId, NodeRunId, OperationId, TaskId, TemplateVersionId

__all__ = [
    "TASK_STATUS_TRANSITIONS",
    "TERMINAL_TASK_STATUSES",
    "DataPolicy",
    "NodeRun",
    "NodeStatus",
    "Task",
    "TaskStatus",
]


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    PARKED = "parked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)

#: Allowed transitions. MVP keeps completed/failed/cancelled terminal; a fresh
#: epoch after failure happens in a *new* task, not by reopening this one.
TASK_STATUS_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.AWAITING_USER,
            TaskStatus.PARKED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.AWAITING_USER: frozenset(
        {TaskStatus.RUNNING, TaskStatus.PARKED, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.PARKED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED_FINAL = "failed_final"
    SKIPPED = "skipped"
    CORRUPT = "corrupt"


class DataPolicy(StrEnum):
    LOCAL_ONLY = "local_only"
    LLM_ALLOWED = "llm_allowed"


class Task(BaseModel):
    """Business-facts row of a task. Body bytes never live here (I4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: TaskId
    request_id: str = Field(min_length=1)
    status: TaskStatus = TaskStatus.CREATED
    data_policy: DataPolicy
    epoch: Epoch = 1
    template_version_id: TemplateVersionId | None = None
    #: active business refs of the current epoch; superseded versions remain
    #: queryable as artifacts, these point at what gates must verify against.
    active_plan_artifact_id: ArtifactId | None = None
    active_binding_artifact_id: ArtifactId | None = None
    active_render_ir_artifact_id: ArtifactId | None = None
    active_candidate_artifact_id: ArtifactId | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES


class NodeRun(BaseModel):
    """One execution attempt of a pipeline node (attempts are separate rows)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_run_id: NodeRunId
    task_id: TaskId
    node_name: str = Field(min_length=1)
    epoch: Epoch
    attempt: int = Field(ge=1)
    status: NodeStatus = NodeStatus.PENDING
    operation_id: OperationId | None = None
    error_code: str | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
