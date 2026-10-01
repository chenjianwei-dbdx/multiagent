"""缺料流：awaiting 展示 → respond 补料 / 省略槽位 → completed。"""

from __future__ import annotations

from typer.testing import CliRunner

from tests.unit.cli.conftest import (
    Workbench,
    kv,
    register_template,
    run_task,
    submit_task,
)

_DECISION = "dec_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"


def _submit_partial(runner: CliRunner, bench: Workbench, request_id: str) -> str:
    version_id = register_template(runner, bench)
    submitted = submit_task(
        runner,
        bench,
        template_version_id=version_id,
        material="partial.md",
        request_id=request_id,
    )
    assert submitted.exit_code == 0, submitted.output
    return kv(submitted.stdout, "task_id")


def test_awaiting_display_then_material_resume_completes(
    runner: CliRunner, bench: Workbench
) -> None:
    task_id = _submit_partial(runner, bench, "req-gap")
    ran = run_task(runner, bench, task_id)
    assert ran.exit_code == 0, ran.output

    # 等待用户输出契约：原因 / 缺槽 / awaiting_event_id / epoch / respond 示例
    assert kv(ran.stdout, "status") == "awaiting_user"
    assert kv(ran.stdout, "missing_slots") == "risks"
    assert kv(ran.stdout, "epoch") == "1"
    assert any(line.startswith("reason:") for line in ran.stdout.splitlines())
    event_id = kv(ran.stdout, "awaiting_event_id")
    assert event_id.startswith("awev_")
    assert f"--awaiting-event {event_id}" in ran.stdout
    assert "--expected-epoch 1" in ran.stdout
    assert "--material" in ran.stdout
    assert "--omit-slot risks" in ran.stdout  # risks 槽 allow_user_omit=True
    # 不得显示成功交付
    assert "delivery_id" not in ran.stdout and "delivery_sha256" not in ran.stdout

    responded = bench.cli(
        runner,
        "task",
        "respond",
        task_id,
        "--decision-id",
        _DECISION,
        "--expected-epoch",
        "1",
        "--awaiting-event",
        event_id,
        "--material",
        str(bench.inputs / "risks.md"),
    )
    assert responded.exit_code == 0, responded.output
    assert kv(responded.stdout, "decision_id") == _DECISION
    assert kv(responded.stdout, "epoch_after") == "2"
    assert kv(responded.stdout, "replayed") == "false"

    resumed = run_task(runner, bench, task_id)
    assert resumed.exit_code == 0, resumed.output
    assert kv(resumed.stdout, "status") == "completed"
    assert kv(resumed.stdout, "delivery_sha256")

    status = bench.cli(runner, "task", "status", task_id)
    assert kv(status.stdout, "status") == "completed"
    assert "missing_slots" not in status.stdout

    # 重复同 decision_id（同 payload）→ 幂等重放
    replay = bench.cli(
        runner,
        "task",
        "respond",
        task_id,
        "--decision-id",
        _DECISION,
        "--expected-epoch",
        "1",
        "--awaiting-event",
        event_id,
        "--material",
        str(bench.inputs / "risks.md"),
    )
    assert replay.exit_code == 0, replay.output
    assert kv(replay.stdout, "replayed") == "true"


def test_omit_slot_branch_completes(runner: CliRunner, bench: Workbench) -> None:
    task_id = _submit_partial(runner, bench, "req-omit")
    ran = run_task(runner, bench, task_id)
    event_id = kv(ran.stdout, "awaiting_event_id")

    responded = bench.cli(
        runner,
        "task",
        "respond",
        task_id,
        "--decision-id",
        _DECISION,
        "--expected-epoch",
        "1",
        "--awaiting-event",
        event_id,
        "--omit-slot",
        "risks",
    )
    assert responded.exit_code == 0, responded.output
    assert kv(responded.stdout, "epoch_after") == "1"  # omit 不推进 epoch

    resumed = run_task(runner, bench, task_id)
    assert resumed.exit_code == 0, resumed.output
    assert kv(resumed.stdout, "status") == "completed"

    exported = bench.cli(
        runner,
        "task",
        "export",
        task_id,
        "--request-id",
        "exp-omit",
        "--output",
        str(bench.inputs / "omitted.docx"),
    )
    assert exported.exit_code == 0, exported.output
    assert (bench.inputs / "omitted.docx").is_file()


def test_omit_slot_of_disallowed_slot_is_rejected(runner: CliRunner, bench: Workbench) -> None:
    version_id = register_template(runner, bench)
    sales_only = bench.inputs / "sales_only.md"
    sales_only.write_text("# 项目周报材料\n\n## 一、本周销售情况\n\n平稳。\n", encoding="utf-8")
    submitted = submit_task(
        runner,
        bench,
        template_version_id=version_id,
        material="sales_only.md",
        request_id="req-omit-bad",
    )
    task_id = kv(submitted.stdout, "task_id")
    ran = run_task(runner, bench, task_id)
    event_id = kv(ran.stdout, "awaiting_event_id")
    assert set(kv(ran.stdout, "missing_slots").split(", ")) == {"risks", "next_plan"}

    bad = bench.cli(
        runner,
        "task",
        "respond",
        task_id,
        "--decision-id",
        _DECISION,
        "--expected-epoch",
        "1",
        "--awaiting-event",
        event_id,
        "--omit-slot",
        "next_plan",  # allow_user_omit=False
    )
    assert bad.exit_code == 1
    assert "ERROR [" in bad.stderr


def test_respond_stale_epoch_exits_1(runner: CliRunner, bench: Workbench) -> None:
    task_id = _submit_partial(runner, bench, "req-stale")
    ran = run_task(runner, bench, task_id)
    event_id = kv(ran.stdout, "awaiting_event_id")

    stale = bench.cli(
        runner,
        "task",
        "respond",
        task_id,
        "--decision-id",
        _DECISION,
        "--expected-epoch",
        "9",
        "--awaiting-event",
        event_id,
        "--material",
        str(bench.inputs / "risks.md"),
    )
    assert stale.exit_code == 1
    assert "CONCURRENCY_CONFLICT" in stale.stderr
    assert "Traceback" not in stale.output


def test_respond_requires_exactly_one_action(runner: CliRunner, bench: Workbench) -> None:
    task_id = _submit_partial(runner, bench, "req-xor")
    ran = run_task(runner, bench, task_id)
    event_id = kv(ran.stdout, "awaiting_event_id")
    base = [
        "task",
        "respond",
        task_id,
        "--decision-id",
        _DECISION,
        "--expected-epoch",
        "1",
        "--awaiting-event",
        event_id,
    ]

    both = bench.cli(
        runner,
        *base,
        "--material",
        str(bench.inputs / "risks.md"),
        "--omit-slot",
        "risks",
    )
    assert both.exit_code == 2  # 用法错误

    neither = bench.cli(runner, *base)
    assert neither.exit_code == 2
