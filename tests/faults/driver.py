"""Crash-recovery driver: each phase runs in its own OS process (T05/T21).

phase1: register template, submit a gap task, run to awaiting_user, exit.
phase2: (new process) respond with the missing material, resume, deliver.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.integration.graph.conftest import GraphEnv

from fixtures.weekly_report import build_weekly_report_template
from omas.artifacts.store import ArtifactStore
from omas.domain.ids import TaskId, new_decision_id
from omas.graph.executor import GraphTaskExecutor
from omas.services import MaterialInput, RespondTask, SubmitTask, TaskService
from omas.services.commands import AwaitingEventId
from omas.storage.db import Ledger

MATERIAL_PARTIAL = (
    "# 项目周报材料\n\n## 一、本周销售情况\n\n销售额 500 万，符合预期。\n\n"
    "## 三、下周计划\n\n推进验收。\n"
)
MATERIAL_RISKS = "## 二、风险与依赖\n\n接口联调存在一天延期风险，已登记。"


def build_env(home: str) -> GraphEnv:
    ledger = Ledger.open(Path(home) / "ledger.sqlite3")
    store = ArtifactStore(Path(home))
    env = GraphEnv(Path(home), ledger, store, build_weekly_report_template())
    env.setup_template()
    return env


def main() -> int:
    phase, home = sys.argv[1], sys.argv[2]
    env = build_env(home)
    service = TaskService(env.home, env.store, env.ledger)
    if phase == "phase1":
        receipt = service.submit(
            SubmitTask(
                request_id="req-fault",
                template_version_id=env.version_id,
                intent="生成周报",
                materials=(
                    MaterialInput(filename="week.md", content=MATERIAL_PARTIAL.encode("utf-8")),
                ),
            )
        )
        executor = GraphTaskExecutor(env.deps(drop=set()), env.render_kwargs)
        view = service.run(TaskId(receipt.task_id), executor)
        assert view.awaiting is not None, "phase1 should end awaiting_user"
        print(receipt.task_id)
        return 0
    if phase == "phase2":
        task_id = sys.argv[3]
        view = service.status(TaskId(task_id))
        if view.delivery is not None:
            # idempotent replay of an already-completed flow
            count = env.ledger.connection.execute(
                "SELECT COUNT(*) AS n FROM deliveries"
            ).fetchone()["n"]
            assert count == 1, f"expected exactly one delivery, got {count}"
            print(view.delivery.delivery_id)
            return 0
        assert view.awaiting is not None
        service.respond(
            RespondTask(
                task_id=TaskId(task_id),
                decision_id=new_decision_id(),
                expected_epoch=view.awaiting.epoch,
                awaiting_event_id=AwaitingEventId(view.awaiting.awaiting_event_id),
                action="provide_material",
                materials=(
                    MaterialInput(filename="risks.md", content=MATERIAL_RISKS.encode("utf-8")),
                ),
            )
        )
        executor = GraphTaskExecutor(env.deps(drop=set()), env.render_kwargs)
        done = service.run(TaskId(task_id), executor)
        assert done.delivery is not None, "phase2 should deliver"
        count = env.ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM deliveries"
        ).fetchone()["n"]
        assert count == 1, f"expected exactly one delivery, got {count}"
        print(done.delivery.delivery_id)
        return 0
    print("unknown phase")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
