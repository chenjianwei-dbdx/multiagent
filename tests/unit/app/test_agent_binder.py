"""AgentBinder policy routing: local_only stays offline; llm_allowed runs
the real expert path against a scripted remote model (offline FunctionModel).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from omas.app.agent_binder import AgentBinder
from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import InboxWriter
from omas.config.settings import ModelSettings
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.ids import ArtifactId, TaskId, new_task_id
from omas.domain.task import DataPolicy, Task
from omas.storage.db import Ledger
from omas.tools.materials import MaterialToolkit, RunContext

MATERIAL = (
    "# 材料\n\n## 一、本周销售情况\n\n销售 100 万。\n\n"
    "## 二、风险与依赖\n\n无。\n\n## 三、下周计划\n\n推进。\n"
)
SETTINGS = ModelSettings(
    provider="anthropic",
    model_id="claude-test",
    base_url="https://relay.example.com",
    api_key_env="OMAS_TEST_KEY",
)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.ledger = Ledger.open(self.home / "ledger.sqlite3")
        self.store = ArtifactStore(self.home)

    def make_task(self, policy: DataPolicy, bind: bool = False) -> Task:
        now = datetime.now(UTC)
        task = self.ledger.tasks.create(
            Task(
                task_id=new_task_id(),
                request_id=f"req-{policy.value}-{new_task_id()[:8]}",
                data_policy=policy,
                created_at=now,
                updated_at=now,
            ),
            "d" * 64,
        )
        if bind:
            from omas.domain.ids import new_template_version_id

            version_id = new_template_version_id()
            self.ledger.template_versions.register(
                template_version_id=version_id,
                template_id="weekly-report",
                version=1,
                docx_sha256="a" * 64,
                contract_sha256="b" * 64,
                styles_sha256="c" * 64,
                static_map_sha256="d" * 64,
                extractor_version="t",
                created_at=datetime.now(UTC),
            )
            task = self.ledger.tasks.bind_template(TaskId(task.task_id), version_id)
        return task

    def ingest(self, task_id: str) -> Artifact:
        _raw, cfile, canonical = InboxWriter(self.store).ingest_text(
            task_id, "m.md", MATERIAL.encode("utf-8")
        )
        return self.ledger.artifacts.register(
            Artifact(
                artifact_id=ArtifactId(cfile.name.removesuffix(".txt")),
                task_id=TaskId(task_id),
                kind=ArtifactKind.CANONICAL_TEXT,
                relative_path=cfile.relative_path,
                sha256=canonical.sha256,
                size=canonical.size_bytes,
                source_refs=(),
                created_at=datetime.now(UTC),
            )
        )


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    box = Env(tmp_path)
    yield box
    box.ledger.close()


def _contract():
    from omas.domain.template import TemplateContract, TemplatePackageHashes

    return TemplateContract(
        schema_version=1,
        template_id="weekly-report",
        version=1,
        extractor_version="t",
        hashes=TemplatePackageHashes(
            docx_sha256="a" * 64,
            contract_sha256="b" * 64,
            styles_sha256="c" * 64,
            static_map_sha256="d" * 64,
        ),
        sections=({"section_id": "s1", "slot_ids": ("sales_summary", "risks", "next_plan")},),
        slots=tuple(
            {
                "slot_id": slot,
                "placeholder": "{{ " + slot + " }}",
                "kind": "text_block",
                "required": True,
                "semantic_requirement": semantic,
                "style_key": "body",
            }
            for slot, semantic in (
                ("sales_summary", "本周销售情况"),
                ("risks", "风险与依赖"),
                ("next_plan", "下周计划"),
            )
        ),
    )


def _real_handles(env: Env, task: Task) -> dict[str, list[str]]:
    toolkit = MaterialToolkit(env.ledger, env.store, RunContext(task_id=TaskId(task.task_id)))
    from omas.core import canonicalize, resolve_span

    artifact = env.ledger.connection.execute(
        "SELECT artifact_id FROM artifacts WHERE task_id = ? AND kind = 'canonical_text'",
        (task.task_id,),
    ).fetchone()
    loaded = env.ledger.artifacts.get(ArtifactId(artifact["artifact_id"]))
    canonical = canonicalize(env.store.read_verified(loaded.relative_path, loaded.sha256))
    handles: dict[str, list[str]] = {}
    for slot, marker in (
        ("sales_summary", "销售 100 万"),
        ("risks", "无。"),
        ("next_plan", "推进。"),
    ):
        start = canonical.text.index(marker)
        span = resolve_span(loaded.artifact_id, canonical, start, start + len(marker))
        resolved = toolkit.resolve_span(loaded.artifact_id, start, start + len(marker))
        handles[slot] = [resolved.span_handle]
        _ = span
    return handles


def _scripted_model(slots_args: dict[str, Any]):
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=slots_args)]
        )

    return FunctionModel(fn)


def test_local_only_routes_to_auto_binder(env: Env) -> None:
    task = env.make_task(DataPolicy.LOCAL_ONLY)
    env.ingest(task.task_id)
    built: list = []

    def factory(settings: ModelSettings, gateway: object, policy: DataPolicy) -> object:
        built.append(settings)
        raise AssertionError("local_only must never construct the remote model")

    binder = AgentBinder(env.ledger, env.store, SETTINGS, model_factory=factory)
    binding, artifact = binder.bind(task, _contract())
    assert binding.bindings and artifact.artifact_id
    assert built == []  # zero remote constructions on local_only (T12)


def test_llm_allowed_without_config_rejected(env: Env) -> None:
    task = env.make_task(DataPolicy.LLM_ALLOWED)
    env.ingest(task.task_id)
    binder = AgentBinder(env.ledger, env.store, None)
    from omas.domain.errors import OmasError

    with pytest.raises(OmasError, match="no model is configured"):
        binder.bind(task, _contract())


def test_llm_allowed_runs_experts_and_audits(env: Env) -> None:
    from omas.domain.task import TaskStatus

    task = env.make_task(DataPolicy.LLM_ALLOWED, bind=True)
    task = env.ledger.tasks.update_status(TaskId(task.task_id), TaskStatus.RUNNING)
    env.ingest(task.task_id)
    handles = _real_handles(env, task)
    args = {
        "slots": [
            {"slot_id": "sales_summary", "binding_status": "bound",
             "span_handles": handles["sales_summary"]},
            {"slot_id": "risks", "binding_status": "bound",
             "span_handles": handles["risks"]},
            {"slot_id": "next_plan", "binding_status": "bound",
             "span_handles": handles["next_plan"]},
        ]
    }
    model = _scripted_model(args)
    binder = AgentBinder(
        env.ledger, env.store, SETTINGS, model_factory=lambda *a: model
    )
    binding, _artifact = binder.bind(task, _contract())
    assert len(binding.bindings) == 3
    bound = [b for b in binding.bindings if b.binding_status == "bound"]
    assert len(bound) == 3
    row = env.ledger.connection.execute(
        "SELECT COUNT(*) AS n FROM llm_calls WHERE task_id = ?", (task.task_id,)
    ).fetchone()
    assert row["n"] >= 1  # audit trail exists; tokens honestly null
