"""Built-in template seeding (ADR 0003).

The three generic report templates (工作报告 / 月报 / 调研报告) ship inside the
wheel as package data under ``src/omas/templates/builtin/`` (manifest JSON +
DOCX bytes; hatchling includes everything under ``src/omas``, cf.
``storage/migrations`` and ``web/templates``). This module is the runtime
consumer of those assets.

Seeding is a **system write**, not a user write: it has no user-supplied
idempotency key and does not participate in the TaskService
``IDEMPOTENCY_CONFLICT`` protocol (AGENTS.md I9 scopes that to user
operations). Its idempotency contract is content-based
(:meth:`omas.templates.registry.TemplateRegistry.ensure_registered`):

- the latest registered version of a template already carries every digest
  the shipped assets produce ⇒ the existing version is returned and
  **nothing is written** (repeat runs are free);
- any mismatch (upgraded assets, extractor change, user content registered
  under the same ``template_id``) ⇒ a new immutable version (never an
  overwrite, I10).

Seeding never calls an LLM and stays local_only: it reuses the exact
scaffold payload construction the web upload path uses, so built-in and
uploaded templates are indistinguishable at the contract level.

Entry points: the ``omas template seed`` CLI command and the ``omas web``
factory (:func:`omas.web.app.create_web_app`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omas.artifacts.store import ArtifactStore
from omas.storage.db import Ledger

from .scaffold import scaffold_payloads

__all__ = [
    "BUILTIN_DIR",
    "MANIFEST_FILE",
    "BuiltinSpec",
    "SeedResult",
    "load_builtin_specs",
    "seed_builtin_templates",
]

#: Package data directory: ``builtin-templates.json`` + one DOCX per template.
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
MANIFEST_FILE = "builtin-templates.json"


@dataclass(frozen=True, slots=True)
class BuiltinSpec:
    """One shipped template: registration inputs plus its DOCX bytes."""

    template_id: str
    display_name: str
    description: str
    docx: bytes
    slot_semantics: dict[str, str]


@dataclass(frozen=True, slots=True)
class SeedResult:
    """Outcome of seeding one template (reported verbatim by the CLI)."""

    template_id: str
    version: int
    version_id: str
    newly_registered: bool
    activatable: bool
    findings: tuple[str, ...] = field(default=())


def load_builtin_specs() -> tuple[BuiltinSpec, ...]:
    """Read the shipped manifest and its DOCX bytes from the package.

    Raises :class:`ValueError` if the packaged manifest is malformed — a
    broken package should fail loudly wherever it is consumed.
    """
    manifest_path = BUILTIN_DIR / MANIFEST_FILE
    if not manifest_path.is_file():
        raise ValueError(f"built-in manifest missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"built-in manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, list):
        raise ValueError(f"built-in manifest must be a list, got {type(manifest).__name__}")
    specs = [_load_spec(entry, index) for index, entry in enumerate(manifest)]
    if not specs:
        raise ValueError("built-in manifest is empty")
    return tuple(specs)


def seed_builtin_templates(
    *,
    store: ArtifactStore,
    ledger: Ledger,
    specs: tuple[BuiltinSpec, ...] | None = None,
) -> tuple[SeedResult, ...]:
    """Seed the built-in templates into one OMAS_HOME (idempotent, LLM-free).

    *specs* defaults to :func:`load_builtin_specs`; tests inject synthetic
    specs (including deliberately broken assets) to exercise the full path.
    Display metadata is written only when a version is actually registered
    (``template_meta`` is mutable by design, but a console rename must
    survive restarts on an unchanged package).
    """
    from .registry import TemplateRegistry

    resolved = load_builtin_specs() if specs is None else specs
    registry = TemplateRegistry(store, ledger)
    results: list[SeedResult] = []
    for spec in resolved:
        payloads = scaffold_payloads(
            spec.docx,
            template_id=spec.template_id,
            slot_semantics=spec.slot_semantics,
        )
        version_id, contract, newly_registered = registry.ensure_registered(
            docx_bytes=payloads.docx_bytes,
            sidecar=payloads.sidecar,
            styles_spec=payloads.styles_spec,
            static_map=payloads.static_map,
            template_id=spec.template_id,
        )
        if newly_registered:
            # Meta ships with the version: only written when a version is
            # actually registered, so a user's console rename of a built-in
            # template survives restarts on an unchanged package (the meta API
            # is mutable by design; artifact versions never are).
            ledger.template_meta.upsert(
                template_id=spec.template_id,
                display_name=spec.display_name or spec.template_id,
                description=spec.description,
                at=datetime.now(UTC),
            )
        results.append(
            SeedResult(
                template_id=spec.template_id,
                version=contract.version,
                version_id=version_id,
                newly_registered=newly_registered,
                activatable=contract.is_activatable(),
                findings=tuple(
                    f"{f.code}@{f.locator or ''}" for f in contract.unsupported_findings
                ),
            )
        )
    return tuple(results)


# ------------------------------------------------------------------- manifest


def _load_spec(entry: Any, index: int) -> BuiltinSpec:
    def require(field_name: str) -> Any:
        if not isinstance(entry, dict) or field_name not in entry:
            raise ValueError(f"manifest[{index}]: missing {field_name!r}")
        return entry[field_name]

    template_id = require("template_id")
    display_name = require("display_name")
    description = require("description")
    docx_name = require("docx")
    semantics = require("slot_semantics")
    tid = template_id if isinstance(template_id, str) else None
    if tid is None or not tid.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"manifest[{index}]: template_id must be alphanumeric/-_")
    if not isinstance(display_name, str) or not isinstance(description, str):
        raise ValueError(f"manifest[{index}]: display_name and description must be strings")
    if (
        not isinstance(docx_name, str)
        or Path(docx_name).name != docx_name
        or not docx_name.endswith(".docx")
    ):
        raise ValueError(f"manifest[{index}]: docx must be a plain .docx file name")
    if not isinstance(semantics, dict) or not semantics:
        raise ValueError(f"manifest[{index}]: slot_semantics must be a non-empty object")
    normalized = {
        str(key): str(value)
        for key, value in semantics.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }
    if len(normalized) != len(semantics):
        raise ValueError(f"manifest[{index}]: slot_semantics keys/values must be non-empty strings")
    docx_path = BUILTIN_DIR / docx_name
    if not docx_path.is_file():
        raise ValueError(f"manifest[{index}]: shipped DOCX missing: {docx_path}")
    return BuiltinSpec(
        template_id=template_id,
        display_name=display_name,
        description=description,
        docx=docx_path.read_bytes(),
        slot_semantics=normalized,
    )
