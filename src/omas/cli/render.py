"""Human/JSON rendering of service DTOs (v1.1 §5 receipt shapes).

Rules encoded here (G 段验收):

- awaiting_user output always carries 原因 / 缺槽列表 / awaiting_event_id /
  当前 epoch / 可复制的 respond 示例命令, and never shows a delivery;
- delivery information only appears for completed tasks;
- JSON output is the TaskView serialization itself, no re-invention.
"""

from __future__ import annotations

import json
from typing import Any

import typer

from omas.domain.ids import new_decision_id
from omas.services.commands import EventPage, TaskView


def echo_kv(key: str, value: Any) -> None:
    typer.echo(f"{key}: {value}")


def echo_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def echo_task_view_json(view: TaskView) -> None:
    echo_json(view.model_dump(mode="json"))


def echo_task_view_human(
    view: TaskView,
    *,
    program: str = "omas",
    omissible_slots: frozenset[str] = frozenset(),
) -> None:
    """Human-readable task state (no body text, no artifact paths)."""
    task = view.task
    echo_kv("task_id", task.task_id)
    echo_kv("status", task.status.value)
    echo_kv("epoch", task.epoch)
    echo_kv("data_policy", task.data_policy.value)
    if task.template_version_id:
        echo_kv("template_version_id", task.template_version_id)
    echo_kv("materials", view.material_count)
    if view.awaiting is not None and task.status.value == "awaiting_user":
        _echo_awaiting(view, program=program, omissible_slots=omissible_slots)
    elif view.delivery is not None:
        echo_kv("delivery_id", view.delivery.delivery_id)
        echo_kv("delivery_sha256", view.delivery.final_sha256)


def _echo_awaiting(
    view: TaskView, *, program: str, omissible_slots: frozenset[str]
) -> None:
    awaiting = view.awaiting
    if awaiting is None:  # pragma: no cover - caller guards
        return
    missing = list(awaiting.missing_slot_ids)
    echo_kv("reason", f"缺少必需槽位材料（missing_material），共 {len(missing)} 个槽位")
    echo_kv("missing_slots", ", ".join(missing))
    echo_kv("awaiting_event_id", awaiting.awaiting_event_id)
    echo_kv("epoch", awaiting.epoch)
    typer.echo("等待用户输入（尚未交付）。可用回应命令：")
    base = (
        f"{program} task respond {view.task.task_id}"
        f" --expected-epoch {awaiting.epoch}"
        f" --awaiting-event {awaiting.awaiting_event_id}"
    )
    typer.echo(
        f"  {base} --decision-id {new_decision_id()} --material <材料.md>"
    )
    if omissible_slots:
        slot = sorted(omissible_slots)[0]
        typer.echo(
            f"  {base} --decision-id {new_decision_id()} --omit-slot {slot}"
            "   # 仅限允许用户省略的槽位："
            + ",".join(sorted(omissible_slots))
        )


def echo_run_outcome_human(
    view: TaskView,
    *,
    error_code: str | None,
    program: str = "omas",
    omissible_slots: frozenset[str] = frozenset(),
) -> None:
    """Result of ``omas task run``: 状态 + 关键事实 + 下一步命令."""
    task = view.task
    echo_kv("task_id", task.task_id)
    echo_kv("status", task.status.value)
    if view.awaiting is not None and task.status.value == "awaiting_user":
        _echo_awaiting(view, program=program, omissible_slots=omissible_slots)
        return
    if task.status.value == "completed" and view.delivery is not None:
        echo_kv("delivery_id", view.delivery.delivery_id)
        echo_kv("delivery_sha256", view.delivery.final_sha256)
        typer.echo(
            f"已交付。导出：{program} task export {task.task_id}"
            " --request-id <id> --output <输出.docx>"
        )
        return
    if task.status.value == "failed":
        echo_kv("error_code", error_code or "UNKNOWN")
        typer.echo(
            f"排查：{program} task events {task.task_id} ；修复后可"
            f" {program} task recover {task.task_id} --request-id <id>"
        )
        return
    echo_kv("epoch", task.epoch)


def echo_events(page: EventPage) -> None:
    for item in page.items:
        refs = " ".join(f"{key}={value}" for key, value in sorted(item.refs.items()))
        counts = " ".join(f"{key}={value}" for key, value in sorted(item.counts.items()))
        parts = [f"seq:{item.seq}", f"event:{item.event_code}"]
        if counts:
            parts.append(f"counts:{counts}")
        if refs:
            parts.append(f"refs:{refs}")
        parts.append(f"at:{item.created_at}")
        typer.echo(" ".join(parts))
    echo_kv("last_seq", page.last_seq)
