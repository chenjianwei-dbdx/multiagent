"""Program-generated identifiers.

All IDs are issued by OMAS code, never accepted from external input or model
output. Factories produce ``<prefix>_<32 hex>`` values (uuid4 based).
"""

from __future__ import annotations

import re
import uuid
from typing import NewType

TaskId = NewType("TaskId", str)
ArtifactId = NewType("ArtifactId", str)
OperationId = NewType("OperationId", str)
NodeRunId = NewType("NodeRunId", str)
TemplateVersionId = NewType("TemplateVersionId", str)
DecisionId = NewType("DecisionId", str)
AwaitingEventId = NewType("AwaitingEventId", str)
DeliveryId = NewType("DeliveryId", str)
SpanHandle = NewType("SpanHandle", str)

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*_[0-9a-f]{32}$")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def new_task_id() -> TaskId:
    return TaskId(new_id("task"))


def new_artifact_id() -> ArtifactId:
    return ArtifactId(new_id("art"))


def new_operation_id() -> OperationId:
    return OperationId(new_id("op"))


def new_node_run_id() -> NodeRunId:
    return NodeRunId(new_id("run"))


def new_template_version_id() -> TemplateVersionId:
    return TemplateVersionId(new_id("tver"))


def new_decision_id() -> DecisionId:
    return DecisionId(new_id("dec"))


def new_awaiting_event_id() -> AwaitingEventId:
    return AwaitingEventId(new_id("awev"))


def new_delivery_id() -> DeliveryId:
    return DeliveryId(new_id("dlv"))


def new_span_handle() -> SpanHandle:
    return SpanHandle(new_id("span"))


def is_valid_id(value: str) -> bool:
    """Structural check for program-issued IDs (prefix + uuid4 hex)."""
    return bool(_ID_PATTERN.match(value))
