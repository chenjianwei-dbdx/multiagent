"""P4 graph flow: interrupt on gap → respond → resume → single delivery."""

from __future__ import annotations

from omas.domain.ids import TaskId, new_decision_id
from omas.domain.task import TaskStatus
from omas.graph.executor import GraphTaskExecutor
from omas.services import MaterialInput, RespondTask, SubmitTask, TaskService
from omas.services.commands import AwaitingEventId

MATERIAL_PARTIAL = (
    "# 项目周报材料\n\n## 一、本周销售情况\n\n销售额 500 万，符合预期。\n\n"
    "## 三、下周计划\n\n推进验收。\n"
)
MATERIAL_RISKS = "## 二、风险与依赖\n\n接口联调存在一天延期风险，已登记。"


def _submit(env, material: str, request_id="req-g1"):
    service = TaskService(env.home, env.store, env.ledger)
    return service.submit(
        SubmitTask(
            request_id=request_id,
            template_version_id=env.version_id,
            intent="生成周报",
            materials=(MaterialInput(filename="week.md", content=material.encode("utf-8")),),
        )
    ), service


def test_interrupt_then_resume_completes(graph_env) -> None:
    env = graph_env
    receipt, service = _submit(env, MATERIAL_PARTIAL)
    task_id = TaskId(receipt.task_id)

    executor = GraphTaskExecutor(env.deps(drop=set()), env.render_kwargs)
    view = service.run(task_id, executor)
    assert view.task.status is TaskStatus.AWAITING_USER
    assert view.awaiting is not None and "risks" in view.awaiting.missing_slot_ids
    assert executor is not None

    event_id = view.awaiting.awaiting_event_id
    answer = service.respond(
        RespondTask(
            task_id=task_id,
            decision_id=new_decision_id(),
            expected_epoch=1,
            awaiting_event_id=AwaitingEventId(event_id),
            action="provide_material",
            materials=(MaterialInput(filename="risks.md", content=MATERIAL_RISKS.encode("utf-8")),),
        )
    )
    assert answer.epoch_after == 2

    done = service.run(task_id, executor)
    assert done.task.status is TaskStatus.COMPLETED
    assert done.delivery is not None
    assert env.store.exists(env.store.delivery_relative(receipt.task_id, f"{receipt.task_id}.docx"))
    count = env.ledger.connection.execute(
        "SELECT COUNT(*) AS n FROM deliveries"
    ).fetchone()["n"]
    assert count == 1


def test_completed_flow_without_gap(graph_env) -> None:
    env = graph_env
    full = (
        "# 项目周报材料\n\n## 一、本周销售情况\n\n销售平稳。\n\n"
        "## 二、风险与依赖\n\n无重大风险。\n\n## 三、下周计划\n\n按计划推进。\n"
    )
    receipt, service = _submit(env, full, request_id="req-g2")
    executor = GraphTaskExecutor(env.deps(drop=set()), env.render_kwargs)
    view = service.run(TaskId(receipt.task_id), executor)
    assert view.task.status is TaskStatus.COMPLETED
    assert view.delivery is not None


def test_repeated_run_is_idempotent(graph_env) -> None:
    env = graph_env
    full = (
        "# 项目周报材料\n\n## 一、本周销售情况\n\n销售平稳。\n\n"
        "## 二、风险与依赖\n\n无重大风险。\n\n## 三、下周计划\n\n按计划推进。\n"
    )
    receipt, service = _submit(env, full, request_id="req-g3")
    executor = GraphTaskExecutor(env.deps(drop=set()), env.render_kwargs)
    first = service.run(TaskId(receipt.task_id), executor)
    second = service.run(TaskId(receipt.task_id), executor)
    assert first.delivery is not None and second.delivery is not None
    assert second.delivery.delivery_id == first.delivery.delivery_id
    count = env.ledger.connection.execute(
        "SELECT COUNT(*) AS n FROM deliveries"
    ).fetchone()["n"]
    assert count == 1
