"""Policy-aware collaborators: local_only stays deterministic, llm_allowed
runs the real experts through the guarded remote model (ADR D19/D25).

- ``local_only`` tasks NEVER construct the remote model; AutoBinder (local,
  deterministic, zero egress) serves them.
- ``llm_allowed`` tasks run Assembler + BindingService over the configured
  Anthropic-format endpoint, gateway-guarded per call.
- every model call leaves an ``llm_calls`` audit row (tokens null when the
  adapter does not report them — never faked as zero).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic_ai.models import Model

from omas.app.auto_binder import AutoBinder
from omas.artifacts.store import ArtifactStore
from omas.config.settings import ModelSettings, SearchSettings
from omas.domain.artifact import Artifact
from omas.domain.errors import OmasError
from omas.domain.ids import TaskId
from omas.domain.ir import BindingIR, ContentPlanIR
from omas.domain.task import DataPolicy, Task
from omas.domain.template import TemplateContract
from omas.security.model_gateway import ModelGateway
from omas.services.binding_service import BindingService
from omas.storage.db import Ledger
from omas.tools.materials import Budget, MaterialToolkit, RunContext

ModelFactory = Callable[[ModelSettings, ModelGateway, DataPolicy], Model]


def _adaptive_budget(ledger: Ledger, task_id: TaskId) -> Budget:
    """Scale the assembler's tool budget with the material count (ADR D27).

    Research-fetched tasks carry many more materials than the 20-call
    baseline was sized for; the caps stay bounded so a runaway model still
    hits a wall. Defaults are unchanged for the pre-research case.
    """
    from omas.tools.materials import Budget

    row = ledger.connection.execute(
        "SELECT COUNT(*) AS n FROM artifacts WHERE task_id = ? AND kind = 'canonical_text'",
        (str(task_id),),
    ).fetchone()
    materials = int(row["n"]) if row is not None else 0
    calls = min(20 + 6 * max(0, materials - 1), 80)
    read_chars = min(40_000 + 8_000 * max(0, materials - 1), 160_000)
    return Budget(max_tool_calls_per_turn=calls, total_read_chars=read_chars)


class AgentBinder:
    """BinderLike implementation routing by the task's data policy."""

    def __init__(
        self,
        ledger: Ledger,
        store: ArtifactStore,
        settings: ModelSettings | None,
        gateway: ModelGateway | None = None,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self._ledger = ledger
        self._store = store
        self._settings = settings
        self._gateway = gateway or ModelGateway()
        self._model_factory = model_factory
        self._auto = AutoBinder(ledger, store)

    def bind(self, task: Task, contract: TemplateContract) -> tuple[BindingIR, Artifact]:
        if task.data_policy is not DataPolicy.LLM_ALLOWED:
            return self._auto.bind(task, contract)
        committed = self._committed_binding(task)
        if committed is not None:
            # same-epoch reuse: the graph's assemble_bind and render tail both
            # call bind(); re-running the remote model would double the cost
            # and cannot change the committed business facts (v1.1 §8.1)
            return committed
        if self._settings is None:
            raise OmasError(
                "task requires llm_allowed but no model is configured (models.toml)"
            )
        from omas.security.model_gateway import ModelGateway

        gateway = ModelGateway(policy=task.data_policy)
        model = self._build_model(task, gateway)
        from pydantic_ai import Agent

        from omas.agents.assembler import (
            ASSEMBLER_SYSTEM_PROMPT,
            Assembler,
            AssemblerProposal,
            assembler_tools,
        )

        toolkit = MaterialToolkit(
            self._ledger,
            self._store,
            RunContext(task_id=task.task_id, epoch=task.epoch),
            budget=_adaptive_budget(self._ledger, task.task_id),
        )
        assembler = Assembler(gateway=gateway)
        endpoint = self._settings

        def agent_factory() -> Agent[Any, AssemblerProposal]:
            return Agent(
                model=model,
                output_type=AssemblerProposal,
                retries=2,
                system_prompt=ASSEMBLER_SYSTEM_PROMPT,
                tools=list(assembler_tools(toolkit)),
            )

        self._guard(endpoint, task)
        proposal = assembler.assemble(
            task=task, plan=None, contract=contract, toolkit=toolkit,
            agent_factory=agent_factory,
        )
        usage = getattr(assembler, "last_usage", None)
        self._record_call(
            endpoint,
            task,
            "assemble_bind",
            tokens_in=getattr(usage, "input_tokens", None) if usage else None,
            tokens_out=getattr(usage, "output_tokens", None) if usage else None,
        )
        service = BindingService(self._ledger, self._store)
        plan_artifact = self._ledger.connection.execute(
            "SELECT artifact_id FROM artifacts WHERE task_id = ? AND kind = 'plan_ir'"
            " ORDER BY created_at DESC LIMIT 1",
            (task.task_id,),
        ).fetchone()
        plan_artifact_id = plan_artifact["artifact_id"] if plan_artifact is not None else None
        if plan_artifact_id is None:
            row = self._ledger.connection.execute(
                "SELECT artifact_id FROM artifacts WHERE task_id = ?"
                " AND kind = 'canonical_text' ORDER BY created_at LIMIT 1",
                (task.task_id,),
            ).fetchone()
            plan_artifact_id = row["artifact_id"] if row is not None else ""
        from omas.domain.ids import ArtifactId as _ArtifactId

        if plan_artifact_id:
            fallback = _ArtifactId(plan_artifact_id)
        else:
            fallback = _fallback_artifact(self._ledger, task)
        return service.commit_proposal(
            task=task,
            proposal=proposal,
            plan=None,
            plan_artifact_id=fallback,
            contract=contract,
        )

    def _committed_binding(self, task: Task) -> tuple[BindingIR, Artifact] | None:
        """Reuse the binding committed for this epoch, if any (no model call)."""
        existing = self._ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM bindings WHERE task_id = ? AND epoch = ?",
            (task.task_id, task.epoch),
        ).fetchone()
        if existing is None or existing["n"] == 0:
            return None
        row = self._ledger.connection.execute(
            "SELECT relative_path FROM artifacts WHERE task_id = ?"
            " AND kind = 'binding_ir' ORDER BY created_at DESC LIMIT 1",
            (task.task_id,),
        ).fetchone()
        if row is None:
            return None
        artifact = self._ledger.artifacts.get_by_path(row["relative_path"])
        if artifact is None:
            return None
        binding = BindingIR.model_validate_json(
            self._store.read_verified(artifact.relative_path, artifact.sha256)
        )
        return binding, artifact

    def _build_model(self, task: Task, gateway: ModelGateway | None = None) -> Model:
        assert self._settings is not None
        target = gateway or self._gateway
        if self._model_factory is not None:
            return self._model_factory(self._settings, target, task.data_policy)
        from omas.security.provider_factory import build_model

        return build_model(self._settings, gateway=target, policy=task.data_policy)

    def _guard(self, settings: ModelSettings, task: Task) -> None:
        from omas.security.model_gateway import ModelGateway
        from omas.security.provider_factory import endpoint_of

        gateway = ModelGateway(policy=task.data_policy)
        gateway.guard_request(endpoint_of(settings), {"phase": "agent_call"})

    def _record_call(
        self,
        settings: ModelSettings,
        task: Task,
        node: str = "plan",
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        stamp = int(datetime.now(UTC).timestamp())
        call_id = f"llm_{task.task_id.removeprefix('task_')}_{node}_{stamp}"
        self._ledger.llm_calls.insert(
            call_id=call_id,
            provider=settings.provider,
            model_id=settings.model_id,
            task_id=task.task_id,
            node_name=node,
            model_config_json=None,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            created_at=datetime.now(UTC),
        )


class PolicyPlanner:
    """PlannerLike: local_only tasks get no LLM plan; llm_allowed run Planner."""

    def __init__(
        self,
        ledger: Ledger,
        settings: ModelSettings | None,
        gateway: ModelGateway | None = None,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self._ledger = ledger
        self._settings = settings
        self._gateway = gateway or ModelGateway(policy=DataPolicy.LLM_ALLOWED)
        self._model_factory = model_factory

    def plan(
        self,
        *,
        intent: str,
        contract: TemplateContract,
        task_id: object,
        template_version_id: object,
    ) -> ContentPlanIR:
        from omas.domain.ids import TaskId

        task = self._ledger.tasks.get(TaskId(str(task_id)))
        if task is None or task.data_policy is not DataPolicy.LLM_ALLOWED:
            # local_only: a full-coverage skeleton plan, no LLM involved
            sections = tuple(
                {"section_id": section.section_id, "slot_ids": list(section.slot_ids)}
                for section in contract.sections
            )
            return ContentPlanIR(
                schema_version=1,
                task_id=TaskId(str(task_id)),
                template_version_id=template_version_id,  # type: ignore[arg-type]
                sections=sections,  # type: ignore[arg-type]
            )
        if self._settings is None:
            raise OmasError("task requires llm_allowed but no model is configured")
        model = (
            self._model_factory(self._settings, self._gateway, task.data_policy)
            if self._model_factory is not None
            else _build(self._settings, self._gateway, task.data_policy)
        )
        from omas.agents.planner import Planner

        planner = Planner()
        from omas.domain.ids import TaskId as _TaskId
        from omas.domain.ids import TemplateVersionId as _TVer

        result = planner.plan(
            intent=intent,
            contract=contract,
            task_id=_TaskId(str(task_id)),
            template_version_id=_TVer(str(template_version_id)),
            agent_factory=lambda: _planner_agent(model),
        )
        usage = getattr(planner, "last_usage", None)
        self._record_call(
            self._settings,
            task,
            tokens_in=getattr(usage, "input_tokens", None) if usage else None,
            tokens_out=getattr(usage, "output_tokens", None) if usage else None,
        )
        return result

    def _record_call(
        self,
        settings: ModelSettings,
        task: Task,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        stamp = int(datetime.now(UTC).timestamp())
        call_id = f"llm_{task.task_id.removeprefix('task_')}_plan_{stamp}"
        self._ledger.llm_calls.insert(
            call_id=call_id,
            provider=settings.provider,
            model_id=settings.model_id,
            task_id=task.task_id,
            node_name="plan",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            created_at=datetime.now(UTC),
        )


def _build(settings: ModelSettings, gateway: ModelGateway, policy: DataPolicy) -> Model:
    from omas.security.provider_factory import build_model

    return build_model(settings, gateway=gateway, policy=policy)


def _planner_agent(model: Model) -> Any:
    from pydantic_ai import Agent

    from omas.agents.planner import PLANNER_SYSTEM_PROMPT, PlannerPlan

    return Agent(
        model=model,
        output_type=PlannerPlan,
        retries=2,
        system_prompt=PLANNER_SYSTEM_PROMPT,
    )


def _fallback_artifact(ledger: Ledger, task: Task) -> Any:
    from omas.domain.ids import ArtifactId

    row = ledger.connection.execute(
        "SELECT artifact_id FROM artifacts WHERE task_id = ?"
        " AND kind = 'canonical_text' ORDER BY created_at LIMIT 1",
        (task.task_id,),
    ).fetchone()
    if row is not None:
        return ArtifactId(row["artifact_id"])
    raise OmasError("task has no artifacts to reference as plan")


class PolicyResearcher:
    """ResearcherLike: web gathering for llm_allowed tasks only (ADR D27).

    ``local_only`` tasks never reach the remote model — the node gates on
    policy before this object is even consulted, and ``research`` re-checks
    the task policy as defense in depth. The LLM only picks URLs; the bytes
    are fetched and registered by :class:`omas.services.research.ResearchService`,
    so no model output can become body text (I2 unchanged).
    """

    def __init__(
        self,
        ledger: Ledger,
        store: ArtifactStore,
        settings: ModelSettings | None,
        search_settings: SearchSettings | None = None,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self._ledger = ledger
        self._store = store
        self._settings = settings
        self._search_settings = search_settings
        self._model_factory = model_factory

    def research(self, task: Task, contract: TemplateContract, intent: str) -> int:
        from omas.domain.task import DataPolicy
        from omas.security.model_gateway import ModelGateway

        if task.data_policy is not DataPolicy.LLM_ALLOWED:
            return 0  # local_only: 零外发，走用户补料的既有路径
        if self._settings is None:
            return 0  # 无模型配置：退化（节点上层的操作行不会落 committed）

        from omas.agents.research import ResearchAgent
        from omas.security.provider_factory import build_model
        from omas.services.research import ResearchService

        gateway = ModelGateway(policy=DataPolicy.LLM_ALLOWED)
        model = (
            self._model_factory(self._settings, gateway, task.data_policy)
            if self._model_factory is not None
            else build_model(self._settings, gateway=gateway, policy=task.data_policy)
        )
        search_client = None
        if self._search_settings is not None:
            from omas.websearch.client import SearchClient

            search_client = SearchClient(self._search_settings, gateway)
        sink = ResearchService(self._store, self._ledger, str(task.task_id))
        agent = ResearchAgent()

        def factory(**_kwargs: object) -> Any:
            from pydantic_ai import Agent as _Agent

            from omas.agents.research import RESEARCH_SYSTEM_PROMPT, ResearchReport

            return _Agent(
                model=model,
                output_type=ResearchReport,
                retries=2,
                system_prompt=RESEARCH_SYSTEM_PROMPT,
                tools=agent.tools(search_client, gateway, sink),
            )

        report = agent.research(
            intent=intent,
            contract=contract,
            task=task,
            search=search_client,
            gateway=gateway,
            sink=sink,
            agent_factory=factory,
        )
        if report.digest.strip():
            # 研究笔记由程序登记为材料（模型只整理已采集页面的内容），
            # 后续 GATE 流程与用户上传材料完全一致
            sink.register_text(
                label="research-digest", query=intent, text=report.digest
            )
        usage = agent.last_usage
        self._record_call(
            settings=self._settings,
            task=task,
            node="research",
            tokens_in=usage.input_tokens if usage is not None else None,
            tokens_out=usage.output_tokens if usage is not None else None,
        )
        _ = report  # 元数据不入正文；来源计数由 sink 的注册结果决定
        return len(sink.list_sources())

    def _record_call(
        self,
        settings: ModelSettings,
        task: Task,
        node: str = "research",
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        stamp = int(datetime.now(UTC).timestamp())
        call_id = f"llm_{task.task_id.removeprefix('task_')}_{node}_{stamp}"
        self._ledger.llm_calls.insert(
            call_id=call_id,
            provider=settings.provider,
            model_id=settings.model_id,
            task_id=task.task_id,
            node_name=node,
            model_config_json=None,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            created_at=datetime.now(UTC),
        )
