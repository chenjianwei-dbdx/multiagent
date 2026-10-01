"""``omas task`` 子命令：提交、运行、状态、回应、取消、恢复、事件、导出。

CLI 只依赖 AppContainer / TaskService；正文与文件字节永远不进入输出
（TaskView 只含 refs 与计数）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from omas.cli.context import cli_context
from omas.cli.render import (
    echo_events,
    echo_kv,
    echo_run_outcome_human,
    echo_task_view_human,
    echo_task_view_json,
)
from omas.domain.ids import AwaitingEventId, DecisionId, TaskId, TemplateVersionId
from omas.domain.task import DataPolicy, Task
from omas.services import (
    CancelTask,
    ExecutionOutcome,
    ExportTask,
    MaterialInput,
    RecoverTask,
    RespondTask,
    SubmitTask,
    TaskExecutor,
)

task_app = typer.Typer(help="任务提交、运行、状态与交付。", no_args_is_help=True)

_MaterialOpt = Annotated[
    list[Path] | None,
    typer.Option(
        "--material",
        help="材料 Markdown 文件（可多次提供）。",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
]


class _RecordingExecutor:
    """TaskExecutor 包装：记住最后一次 ExecutionOutcome（failed 时取 error_code）。"""

    def __init__(self, inner: TaskExecutor) -> None:
        self._inner = inner
        self.last_outcome: ExecutionOutcome | None = None

    def execute(self, task: Task) -> ExecutionOutcome:
        outcome = self._inner.execute(task)
        self.last_outcome = outcome
        return outcome


def _materials(paths: list[Path]) -> list[MaterialInput]:
    return [MaterialInput(filename=path.name, content=path.read_bytes()) for path in paths]


@task_app.command("submit")
def submit(
    ctx: typer.Context,
    template: Annotated[
        str, typer.Option("--template", help="模板版本 ID（template extract 的 version_id）。")
    ],
    intent: Annotated[str, typer.Option("--intent", help="用户意图（任务数据）。")],
    request_id: Annotated[
        str, typer.Option("--request-id", help="幂等键（同 key 同 payload 重放）。")
    ],
    material: Annotated[
        list[Path],
        typer.Option(
            "--material",
            help="材料 Markdown 文件（至少一个，可多次提供）。",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    data_policy: Annotated[
        DataPolicy, typer.Option("--data-policy", help="数据策略，默认 local_only。")
    ] = DataPolicy.LOCAL_ONLY,
) -> None:
    """提交任务；打印 task_id（重放时注明 replayed）。"""
    cli = cli_context(ctx)
    with cli.errors():
        receipt = cli.container().service.submit(
            SubmitTask(
                request_id=request_id,
                template_version_id=TemplateVersionId(template),
                intent=intent,
                materials=tuple(_materials(material)),
                data_policy=data_policy,
            )
        )
        echo_kv("task_id", receipt.task_id)
        echo_kv("replayed", str(receipt.replayed).lower())
        if receipt.replayed:
            typer.echo(f"# 幂等重放：request_id 已存在，返回原任务 {receipt.task_id}")


@task_app.command("run")
def run(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="任务 ID。")],
) -> None:
    """驱动一轮执行：completed / awaiting_user（含缺槽与回应示例）/ failed。"""
    cli = cli_context(ctx)
    with cli.errors():
        container = cli.container()
        executor = _RecordingExecutor(container.executor())
        view = container.service.run(TaskId(task_id), executor)
        echo_run_outcome_human(
            view,
            error_code=executor.last_outcome.error_code if executor.last_outcome else None,
            program=cli.program,
            omissible_slots=container.service.omissible_missing_slots(view),
        )


@task_app.command("status")
def status(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="任务 ID。")],
    as_json: Annotated[bool, typer.Option("--json", help="输出 TaskView 的 JSON 序列化。")] = False,
) -> None:
    """查看任务状态（纯读；等待中会给出可复制的 respond 命令）。"""
    cli = cli_context(ctx)
    with cli.errors():
        container = cli.container()
        view = container.service.status(TaskId(task_id))
        if as_json:
            echo_task_view_json(view)
        else:
            echo_task_view_human(
                view,
                program=cli.program,
                omissible_slots=container.service.omissible_missing_slots(view),
            )


@task_app.command("respond")
def respond(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="任务 ID。")],
    decision_id: Annotated[str, typer.Option("--decision-id", help="决策幂等键。")],
    expected_epoch: Annotated[int, typer.Option("--expected-epoch", min=1, help="当前 epoch。")],
    awaiting_event: Annotated[str, typer.Option("--awaiting-event", help="等待事件 ID。")],
    material: _MaterialOpt = None,
    omit_slot: Annotated[
        str | None, typer.Option("--omit-slot", help="省略某槽位（须允许省略）。")
    ] = None,
) -> None:
    """回应等待事件：补料（--material）或省略槽位（--omit-slot），二选一。"""
    cli = cli_context(ctx)
    with cli.errors():
        if material and omit_slot is not None:
            raise typer.BadParameter("--material 与 --omit-slot 互斥，只能选一个")
        if not material and omit_slot is None:
            raise typer.BadParameter("必须提供 --material（至少一个）或 --omit-slot 之一")
        action = "provide_material" if material else "omit_slot"
        receipt = cli.container().service.respond(
            RespondTask(
                task_id=TaskId(task_id),
                decision_id=DecisionId(decision_id),
                expected_epoch=expected_epoch,
                awaiting_event_id=AwaitingEventId(awaiting_event),
                action=action,
                slot_id=omit_slot,
                materials=tuple(_materials(material)) if material else (),
            )
        )
        echo_kv("decision_id", receipt.decision_id)
        echo_kv("epoch_after", receipt.epoch_after)
        echo_kv("replayed", str(receipt.replayed).lower())
        if receipt.replayed:
            typer.echo("# 幂等重放：decision_id 已接受过，返回原 receipt")
        typer.echo(f"# 继续执行：{cli.program} task run {task_id}")


@task_app.command("cancel")
def cancel(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="任务 ID。")],
    request_id: Annotated[str, typer.Option("--request-id", help="幂等键。")],
) -> None:
    """取消任务（幂等；终态任务保持原状态）。"""
    cli = cli_context(ctx)
    with cli.errors():
        container = cli.container()
        view = container.service.cancel(
            CancelTask(task_id=TaskId(task_id), request_id=request_id)
        )
        echo_task_view_human(view, program=cli.program)


@task_app.command("recover")
def recover(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="任务 ID。")],
    request_id: Annotated[str, typer.Option("--request-id", help="幂等键。")],
) -> None:
    """崩溃后恢复：复核已提交产物并修复任务状态（幂等）。"""
    cli = cli_context(ctx)
    with cli.errors():
        container = cli.container()
        view = container.service.recover(
            RecoverTask(task_id=TaskId(task_id), request_id=request_id)
        )
        echo_task_view_human(view, program=cli.program)


@task_app.command("events")
def events(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="任务 ID。")],
    after: Annotated[int, typer.Option("--after", min=0, help="只返回 seq 大于该值的事件。")] = 0,
) -> None:
    """查看任务事件（纯读，--after 分页）。"""
    cli = cli_context(ctx)
    with cli.errors():
        page = cli.container().service.events(TaskId(task_id), after=after)
        echo_events(page)


@task_app.command("export")
def export(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="任务 ID。")],
    request_id: Annotated[str, typer.Option("--request-id", help="幂等键。")],
    output: Annotated[Path, typer.Option("--output", help="导出文件路径（不覆盖既有不同内容）。")],
) -> None:
    """导出已提交交付物；打印路径与 sha256。"""
    cli = cli_context(ctx)
    with cli.errors():
        receipt = cli.container().service.export(
            ExportTask(
                task_id=TaskId(task_id),
                request_id=request_id,
                output_path=str(output),
            )
        )
        echo_kv("exported_to", receipt.exported_to)
        echo_kv("sha256", receipt.sha256)
        echo_kv("delivery_id", receipt.delivery_id)
        echo_kv("replayed", str(receipt.replayed).lower())
