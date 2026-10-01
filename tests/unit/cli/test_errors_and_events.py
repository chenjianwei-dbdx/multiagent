"""错误处理（退出码/stderr 单行/无堆栈）、取消、事件分页、home 解析。"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from omas.cli.main import app
from omas.domain.errors import OmasError
from tests.unit.cli.conftest import (
    Workbench,
    kv,
    register_template,
    run_task,
    submit_task,
)

_UNKNOWN_TASK = "task_ffffffffffffffffffffffffffffffff"
_SEQ_RE = re.compile(r"^seq:(\d+) event:([a-z_]+)", re.MULTILINE)


def test_status_unknown_task_exits_1_single_error_line(
    runner: CliRunner, bench: Workbench
) -> None:
    result = bench.cli(runner, "task", "status", _UNKNOWN_TASK)
    assert result.exit_code == 1
    assert "ERROR [ARTIFACT_NOT_FOUND]" in result.stderr
    assert "Traceback" not in result.output
    assert result.stderr.count("ERROR [") == 1


def test_run_unknown_task_exits_1(runner: CliRunner, bench: Workbench) -> None:
    result = bench.cli(runner, "task", "run", _UNKNOWN_TASK)
    assert result.exit_code == 1
    assert "ERROR [ARTIFACT_NOT_FOUND]" in result.stderr


def test_unknown_template_fails_at_run_with_exit_1(runner: CliRunner, bench: Workbench) -> None:
    """Submit rejects unregistered template versions up front (fixed 2026-09-30:
    the error used to surface only at run time)."""
    submitted = bench.cli(
        runner,
        "task",
        "submit",
        "--template",
        "tver_ffffffffffffffffffffffffffffffff",
        "--intent",
        "生成周报",
        "--material",
        str(bench.inputs / "full.md"),
        "--request-id",
        "req-unknown-template",
    )
    assert submitted.exit_code == 1
    assert "not registered" in submitted.output

def test_debug_flag_reraises_original_error(runner: CliRunner, bench: Workbench) -> None:
    result = runner.invoke(
        app, ["--debug", "--home", str(bench.home), "task", "status", _UNKNOWN_TASK]
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, OmasError)
    assert "ERROR [" not in result.stderr


def test_cancel_then_run_is_rejected(runner: CliRunner, bench: Workbench) -> None:
    version_id = register_template(runner, bench)
    task_id = kv(
        submit_task(
            runner,
            bench,
            template_version_id=version_id,
            material="full.md",
            request_id="req-cancel",
        ).stdout,
        "task_id",
    )
    cancelled = bench.cli(runner, "task", "cancel", task_id, "--request-id", "cancel-1")
    assert cancelled.exit_code == 0, cancelled.output
    assert kv(cancelled.stdout, "status") == "cancelled"

    replay = bench.cli(runner, "task", "cancel", task_id, "--request-id", "cancel-1")
    assert replay.exit_code == 0  # 幂等

    ran = run_task(runner, bench, task_id)
    assert ran.exit_code == 1
    assert "TASK_STATE_INVALID" in ran.stderr


def test_recover_after_completion_is_idempotent(runner: CliRunner, bench: Workbench) -> None:
    version_id = register_template(runner, bench)
    task_id = kv(
        submit_task(
            runner,
            bench,
            template_version_id=version_id,
            material="full.md",
            request_id="req-recover",
        ).stdout,
        "task_id",
    )
    assert run_task(runner, bench, task_id).exit_code == 0
    recovered = bench.cli(runner, "task", "recover", task_id, "--request-id", "rec-1")
    assert recovered.exit_code == 0, recovered.output
    assert kv(recovered.stdout, "status") == "completed"
    again = bench.cli(runner, "task", "recover", task_id, "--request-id", "rec-1")
    assert again.exit_code == 0 and kv(again.stdout, "status") == "completed"


def test_events_and_pagination(runner: CliRunner, bench: Workbench) -> None:
    version_id = register_template(runner, bench)
    task_id = kv(
        submit_task(
            runner,
            bench,
            template_version_id=version_id,
            material="full.md",
            request_id="req-events",
        ).stdout,
        "task_id",
    )
    assert run_task(runner, bench, task_id).exit_code == 0
    exported = bench.cli(
        runner,
        "task",
        "export",
        task_id,
        "--request-id",
        "exp-events",
        "--output",
        str(bench.inputs / "events.docx"),
    )
    assert exported.exit_code == 0, exported.output

    page = bench.cli(runner, "task", "events", task_id)
    assert page.exit_code == 0
    events = _SEQ_RE.findall(page.stdout)
    seqs = [int(seq) for seq, _ in events]
    assert seqs == sorted(seqs) and len(seqs) >= 4
    codes = [code for _, code in events]
    assert codes[0] == "task_submitted"
    assert "exported" in codes
    assert kv(page.stdout, "last_seq") == str(max(seqs))

    middle = seqs[len(seqs) // 2]
    later = bench.cli(runner, "task", "events", task_id, "--after", str(middle))
    assert later.exit_code == 0
    later_seqs = [int(seq) for seq, _ in _SEQ_RE.findall(later.stdout)]
    assert later_seqs and all(seq > middle for seq in later_seqs)

    unknown = bench.cli(runner, "task", "events", _UNKNOWN_TASK)
    assert unknown.exit_code == 1
    assert "ERROR [ARTIFACT_NOT_FOUND]" in unknown.stderr


def test_export_without_delivery_exits_1(runner: CliRunner, bench: Workbench) -> None:
    version_id = register_template(runner, bench)
    task_id = kv(
        submit_task(
            runner,
            bench,
            template_version_id=version_id,
            material="partial.md",
            request_id="req-no-export",
        ).stdout,
        "task_id",
    )
    assert run_task(runner, bench, task_id).exit_code == 0  # awaiting_user
    result = bench.cli(
        runner,
        "task",
        "export",
        task_id,
        "--request-id",
        "exp-nothing",
        "--output",
        str(bench.inputs / "nothing.docx"),
    )
    assert result.exit_code == 1
    assert "no committed delivery" in result.stderr
    assert not (bench.inputs / "nothing.docx").exists()


def test_export_refuses_to_overwrite_different_content(
    runner: CliRunner, bench: Workbench
) -> None:
    version_id = register_template(runner, bench)
    task_id = kv(
        submit_task(
            runner,
            bench,
            template_version_id=version_id,
            material="full.md",
            request_id="req-overwrite",
        ).stdout,
        "task_id",
    )
    assert run_task(runner, bench, task_id).exit_code == 0
    target = bench.inputs / "precious.docx"
    original = "用户已有的不同内容".encode("utf-8")
    target.write_bytes(original)
    result = bench.cli(
        runner, "task", "export", task_id, "--request-id", "exp-overwrite", "--output", str(target)
    )
    assert result.exit_code == 1
    assert "refusing to overwrite" in result.stderr
    assert target.read_bytes() == original  # 未被覆盖


def test_home_resolved_from_env_var(runner: CliRunner, bench: Workbench) -> None:
    version_id = register_template(runner, bench)
    submitted = runner.invoke(
        app,
        [
            "task",
            "submit",
            "--template",
            version_id,
            "--intent",
            "生成周报",
            "--material",
            str(bench.inputs / "full.md"),
            "--request-id",
            "req-env",
        ],
        env={"OMAS_HOME": str(bench.home)},
    )
    assert submitted.exit_code == 0, submitted.output
    task_id = kv(submitted.stdout, "task_id")
    status = runner.invoke(
        app, ["task", "status", task_id], env={"OMAS_HOME": str(bench.home)}
    )
    assert status.exit_code == 0
    assert kv(status.stdout, "task_id") == task_id
