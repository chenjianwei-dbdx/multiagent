"""Composition root: one place where everything is wired together.

The CLI (and later Web) only ever touches the objects built here. In the
current offline deployment (no local model configured yet) the binder is the
deterministic AutoBinder; when a model configuration exists the Assembler +
BindingService path is preferred (ADR D19).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omas.app.agent_binder import AgentBinder, PolicyPlanner, PolicyResearcher
from omas.artifacts.store import ArtifactStore
from omas.config.settings import ModelSettings, SearchSettings
from omas.graph.deps import GraphDeps, RenderKwargs
from omas.graph.executor import GraphTaskExecutor
from omas.services import TaskService
from omas.storage.db import Ledger


@dataclass
class AppContainer:
    home: Path
    store: ArtifactStore
    ledger: Ledger
    service: TaskService
    model_settings: ModelSettings | None = None
    search_settings: SearchSettings | None = None

    def executor(self) -> GraphTaskExecutor:
        binder = AgentBinder(self.ledger, self.store, self.model_settings)
        planner = PolicyPlanner(self.ledger, self.model_settings)
        researcher = PolicyResearcher(
            self.ledger,
            self.store,
            self.model_settings,
            search_settings=self.search_settings,
        )
        deps = GraphDeps(
            home=self.home,
            store=self.store,
            ledger=self.ledger,
            planner=planner,
            binder=binder,
            researcher=researcher,
        )
        return GraphTaskExecutor(deps, self.render_kwargs)

    def render_kwargs(self, task_id: str) -> RenderKwargs | None:
        from omas.domain.ids import TaskId, TemplateVersionId
        from omas.domain.template import StyleSpec

        task = self.ledger.tasks.get(TaskId(task_id))
        if task is None or task.template_version_id is None:
            return None
        from omas.templates.registry import TemplateRegistry

        registry = TemplateRegistry(self.store, self.ledger)
        version_id = TemplateVersionId(task.template_version_id)
        contract = registry.get_contract(version_id)
        row = self.ledger.template_versions.get(version_id)
        version_number = row.version if row is not None else 1
        kwargs = RenderKwargs(
            contract=contract,
            template_version_id=version_id,
            template_docx=registry.load_docx(version_id),
            template_relative_path=self.store.template_relative(
                contract.template_id, str(version_number), "template.docx"
            ),
            static_map=self._load_json(
                self.store.template_relative(
                    contract.template_id, str(version_number), "static-map.json"
                )
            ),
            styles_spec=None,
        )
        styles_raw = self._load_json(
            self.store.template_relative(
                contract.template_id, str(version_number), "styles.json"
            )
        )
        if styles_raw:
            kwargs = RenderKwargs(
                contract=kwargs.contract,
                template_version_id=kwargs.template_version_id,
                template_docx=kwargs.template_docx,
                template_relative_path=kwargs.template_relative_path,
                static_map=kwargs.static_map,
                styles_spec={
                    key: StyleSpec.model_validate({**_as_mapping(value), "style_key": key})
                    for key, value in styles_raw.items()
                },
            )
        return kwargs

    def _load_json(self, relative: str) -> dict[str, object] | None:
        import json

        try:
            data = self.store.read(relative)
        except Exception:
            return None
        try:
            loaded: dict[str, object] = json.loads(data.decode("utf-8"))
            return loaded
        except (ValueError, UnicodeDecodeError):
            return None


def _as_mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def build_app(home: Path, models: object | None = None) -> AppContainer:
    from omas.config.settings import default_models_path, load_models_config

    home.mkdir(parents=True, exist_ok=True)
    if models is None:
        models = load_models_config(default_models_path(home))
    settings = models.endpoint_config() if hasattr(models, "endpoint_config") else None
    search_settings = getattr(models, "search", None)
    ledger = Ledger.open(home / "ledger.sqlite3")
    store = ArtifactStore(home)
    service = TaskService(home, store, ledger)
    return AppContainer(
        home=home,
        store=store,
        ledger=ledger,
        service=service,
        model_settings=settings,
        search_settings=search_settings,
    )
