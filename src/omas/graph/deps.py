"""Collaborator seams the graph wires in (I3: one authoritative writer each).

Planner/Assembler are LLM experts injected by the composition root; the
program-side binder converts proposals into committed BindingIRs. Tests
inject deterministic fakes; production injects the real agents (P2/P3).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from omas.artifacts.store import ArtifactStore
from omas.domain.artifact import Artifact
from omas.domain.ir import BindingIR, ContentPlanIR
from omas.domain.task import Task
from omas.domain.template import TemplateContract
from omas.storage.db import Ledger


class PlannerLike(Protocol):
    def plan(
        self,
        *,
        intent: str,
        contract: TemplateContract,
        task_id: object,
        template_version_id: object,
    ) -> ContentPlanIR: ...


class BinderLike(Protocol):
    """assemble + commit: returns the committed BindingIR and its artifact."""

    def bind(self, task: Task, contract: TemplateContract) -> tuple[BindingIR, Artifact]:
        ...


class ResearcherLike(Protocol):
    """Web gathering before assembly (ADR D27); returns how many sources it added."""

    def research(self, task: Task, contract: TemplateContract, intent: str) -> int: ...


@dataclass(frozen=True, slots=True)
class GraphDeps:
    home: Path
    store: ArtifactStore
    ledger: Ledger
    planner: PlannerLike | None = None
    binder: BinderLike | None = None
    researcher: ResearcherLike | None = None
    intent_loader: Callable[[str], str] | None = None
    render_kwargs_provider: Callable[[str], RenderKwargs | None] | None = None


@dataclass(frozen=True, slots=True)
class RenderKwargs:
    """Everything the deterministic pipeline needs besides task+binding."""

    contract: TemplateContract
    template_version_id: str
    template_docx: bytes
    template_relative_path: str
    static_map: dict[str, object] | None = None
    styles_spec: dict[str, object] | None = None
