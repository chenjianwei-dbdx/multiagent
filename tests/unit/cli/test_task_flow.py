"""submit → run → status → export 主链路 + 提交幂等/冲突。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner, Result

from omas.app.bootstrap import build_app
from omas.domain.ids import TaskId
from tests.unit.cli.conftest import (
    Workbench,
    kv,
    register_template,
    run_task,
    submit_task,
)


def _export(
    runner: CliRunner, bench: Workbench, task_id: str, request_id: str, output: Path
) -> Result:
    return bench.cli(
        runner, "task", "export", task_id, "--request-id", request_id, "--output", str(output)
    )


def test_submit_run_status_export_roundtrip(runner: CliRunner, bench: Workbench) -> None:
    version_id = register_template(runner, bench)

    submitted = submit_task(
        runner, bench, template_version_id=version_id, material="full.md", request_id="req-full"
    )
    assert submitted.exit_code == 0, submitted.output
    task_id = kv(submitted.stdout, "task_id")
    assert task_id.startswith("task_")
    assert kv(submitted.stdout, "replayed") == "false"

    ran = run_task(runner, bench, task_id)
    assert ran.exit_code == 0, ran.output
    assert kv(ran.stdout, "status") == "completed"
    sha = kv(ran.stdout, "delivery_sha256")
    assert "reason:" not in ran.stdout and "missing_slots" not in ran.stdout

    status = bench.cli(runner, "task", "status", task_id)
    assert status.exit_code == 0
    assert kv(status.stdout, "status") == "completed"
    assert kv(status.stdout, "delivery_id").startswith("dlv_")
    assert kv(status.stdout, "materials") == "1"

    # --json 输出必须就是 TaskView 的序列化
    as_json = bench.cli(runner, "task", "status", task_id, "--json")
    assert as_json.exit_code == 0
    container = build_app(bench.home)
    try:
        view = container.service.status(TaskId(task_id))
    finally:
        container.ledger.close()
    assert json.loads(as_json.stdout) == view.model_dump(mode="json")

    delivered = bench.inputs / "delivered.docx"
    exported = _export(runner, bench, task_id, "exp-1", delivered)
    assert exported.exit_code == 0, exported.output
    assert kv(exported.stdout, "sha256") == sha
    assert hashlib.sha256(delivered.read_bytes()).hexdigest() == sha
    assert Path(kv(exported.stdout, "exported_to")) == delivered.resolve()
    assert kv(exported.stdout, "replayed") == "false"

    # 同 request-id 同目标重放导出：幂等，不覆盖
    replay = _export(runner, bench, task_id, "exp-1", delivered)
    assert replay.exit_code == 0, replay.output
    assert kv(replay.stdout, "replayed") == "true"
    assert kv(replay.stdout, "sha256") == sha


def test_run_is_idempotent_after_completion(runner: CliRunner, bench: Workbench) -> None:
    version_id = register_template(runner, bench)
    task_id = kv(
        submit_task(
            runner,
            bench,
            template_version_id=version_id,
            material="full.md",
            request_id="req-rerun",
        ).stdout,
        "task_id",
    )
    first = run_task(runner, bench, task_id)
    second = run_task(runner, bench, task_id)
    assert first.exit_code == 0 and second.exit_code == 0
    assert kv(first.stdout, "delivery_id") == kv(second.stdout, "delivery_id")


def test_submit_replay_returns_same_task_id(runner: CliRunner, bench: Workbench) -> None:
    version_id = register_template(runner, bench)
    first = submit_task(
        runner, bench, template_version_id=version_id, material="full.md", request_id="req-replay"
    )
    replay = submit_task(
        runner, bench, template_version_id=version_id, material="full.md", request_id="req-replay"
    )
    assert replay.exit_code == 0
    assert kv(replay.stdout, "task_id") == kv(first.stdout, "task_id")
    assert kv(replay.stdout, "replayed") == "true"


def test_submit_conflicting_request_id_exits_1(runner: CliRunner, bench: Workbench) -> None:
    version_id = register_template(runner, bench)
    submit_task(
        runner, bench, template_version_id=version_id, material="full.md", request_id="req-conflict"
    )
    conflict = submit_task(
        runner,
        bench,
        template_version_id=version_id,
        material="full.md",
        request_id="req-conflict",
        intent="不同的意图",
    )
    assert conflict.exit_code == 1
    assert "IDEMPOTENCY_CONFLICT" in conflict.stderr
    assert "ERROR [" in conflict.stderr and conflict.stderr.count("ERROR [") == 1
