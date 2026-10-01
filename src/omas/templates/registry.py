"""Versioned template registration over the artifact pool and the ledger
(v1.1 §3.1).

Layout per template version under OMAS_HOME::

    templates/<template_id>/<version>/
        template.docx   contract.json   styles.json   static-map.json   manifest.json

Files are written through :meth:`omas.artifacts.store.ArtifactStore.write_immutable`
(paths from :meth:`ArtifactStore.template_relative`), so a registered version
is immutable by construction: re-registering the same DOCX produces a new
version directory, never an overwrite.

Template files are pool-owned, not task artifacts: ``task_id`` is ``None``
for the template pool and the ``artifacts`` table requires a task-scoped
operation, so registration is recorded in the dedicated ``template_versions``
table only; the store manages the bytes (AGENTS.md I7: file system is the
byte-level source of truth).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from omas.artifacts.store import ArtifactStore
from omas.domain.errors import ArtifactHashMismatchError, ArtifactNotFoundError
from omas.domain.ids import TemplateVersionId, new_template_version_id
from omas.domain.template import TemplateContract
from omas.storage.db import Ledger, utc_now
from omas.storage.repositories import TemplateVersionRow

from .extractor import EXTRACTOR_VERSION, build_contract, canonical_json

__all__ = [
    "CONTRACT_FILE",
    "DOCX_FILE",
    "MANIFEST_FILE",
    "STATIC_MAP_FILE",
    "STYLES_FILE",
    "TemplateRegistry",
]

#: File names inside one template version directory (manifest.json included).
DOCX_FILE = "template.docx"
CONTRACT_FILE = "contract.json"
STYLES_FILE = "styles.json"
STATIC_MAP_FILE = "static-map.json"
MANIFEST_FILE = "manifest.json"

_TEMPLATE_FILES = (DOCX_FILE, CONTRACT_FILE, STYLES_FILE, STATIC_MAP_FILE)


class TemplateRegistry:
    """Registers immutable template versions and serves verified reads."""

    def __init__(self, store: ArtifactStore, ledger: Ledger) -> None:
        self._store = store
        self._ledger = ledger

    # -------------------------------------------------------------- register

    def register(
        self,
        *,
        docx_bytes: bytes,
        sidecar: dict[str, Any],
        styles_spec: dict[str, Any],
        static_map: dict[str, Any],
        template_id: str,
    ) -> tuple[TemplateVersionId, TemplateContract]:
        """Extract, persist and register the next version of *template_id*.

        The version number is ``max(existing versions) + 1`` for this
        template_id. A contract carrying ``unsupported_findings`` is still
        registered (auditability); whether it may be *activated* is decided
        solely by :meth:`TemplateContract.is_activatable`.
        """
        version = self._next_version(template_id)
        contract = build_contract(
            docx_bytes=docx_bytes,
            sidecar=sidecar,
            styles_spec=styles_spec,
            static_map=static_map,
            template_id=template_id,
            version=version,
            extractor_version=EXTRACTOR_VERSION,
        )
        version_id = new_template_version_id()

        payloads: dict[str, bytes] = {
            DOCX_FILE: docx_bytes,
            CONTRACT_FILE: canonical_json(contract.model_dump(mode="json")),
            STYLES_FILE: canonical_json(styles_spec),
            STATIC_MAP_FILE: canonical_json(static_map),
        }
        digests = {
            name: _sha256(payload) for name, payload in payloads.items()
        }
        manifest = {
            "schema_version": 1,
            "template_id": template_id,
            "version": version,
            "template_version_id": version_id,
            "extractor_version": EXTRACTOR_VERSION,
            "contract_schema_version": contract.schema_version,
            "files": digests,
            "docx_sha256": contract.hashes.docx_sha256,
            "contract_sha256": contract.hashes.contract_sha256,
            "styles_sha256": contract.hashes.styles_sha256,
            "static_map_sha256": contract.hashes.static_map_sha256,
            "activatable": contract.is_activatable(),
        }
        payloads[MANIFEST_FILE] = canonical_json(manifest)

        for name, payload in payloads.items():
            self._store.write_immutable(
                self._store.template_relative(template_id, str(version), name), payload
            )

        row = self._ledger.template_versions.register(
            template_version_id=version_id,
            template_id=template_id,
            version=version,
            docx_sha256=contract.hashes.docx_sha256,
            contract_sha256=contract.hashes.contract_sha256,
            styles_sha256=contract.hashes.styles_sha256,
            static_map_sha256=contract.hashes.static_map_sha256,
            extractor_version=EXTRACTOR_VERSION,
            created_at=utc_now(),
        )
        if row.version != version:  # pragma: no cover - defensive
            raise ArtifactHashMismatchError(
                f"ledger recorded version {row.version} for {template_id}, expected {version}"
            )
        return TemplateVersionId(row.template_version_id), contract

    def ensure_registered(
        self,
        *,
        docx_bytes: bytes,
        sidecar: dict[str, Any],
        styles_spec: dict[str, Any],
        static_map: dict[str, Any],
        template_id: str,
    ) -> tuple[TemplateVersionId, TemplateContract, bool]:
        """Register the next version unless the latest one is byte-identical.

        Idempotency probe for built-in seeding (:func:`omas.templates.seed`):
        the contract is rebuilt exactly as it would exist at the latest
        version slot and every recorded digest is compared. A full match
        means the shipped assets already describe this exact package state,
        so the existing version is returned **before any file is written**
        (``write_immutable`` never overwrites; AGENTS.md I10). Any mismatch —
        upgraded assets, extractor change, or user content registered under
        the same ``template_id`` — falls through to a fresh version.

        Returns ``(version_id, contract, newly_registered)``.
        """
        latest = self._ledger.template_versions.latest_for(template_id)
        if latest is not None:
            probe = build_contract(
                docx_bytes=docx_bytes,
                sidecar=sidecar,
                styles_spec=styles_spec,
                static_map=static_map,
                template_id=template_id,
                version=latest.version,
                extractor_version=EXTRACTOR_VERSION,
            )
            if (
                probe.hashes.docx_sha256 == latest.docx_sha256
                and probe.hashes.styles_sha256 == latest.styles_sha256
                and probe.hashes.static_map_sha256 == latest.static_map_sha256
                and probe.hashes.contract_sha256 == latest.contract_sha256
            ):
                existing = TemplateVersionId(latest.template_version_id)
                return existing, self.get_contract(existing), False
        version_id, contract = self.register(
            docx_bytes=docx_bytes,
            sidecar=sidecar,
            styles_spec=styles_spec,
            static_map=static_map,
            template_id=template_id,
        )
        return version_id, contract, True

    # ------------------------------------------------------------------ read

    def get_contract(self, version_id: TemplateVersionId) -> TemplateContract:
        """Load and hash-verify the contract of *version_id*."""
        row = self._require_row(version_id)
        manifest = self._manifest(row)
        contract_bytes = self._store.read_verified(
            self._store.template_relative(row.template_id, str(row.version), CONTRACT_FILE),
            manifest["files"][CONTRACT_FILE],
        )
        contract = TemplateContract.model_validate_json(contract_bytes)
        if contract.hashes.contract_sha256 != row.contract_sha256:
            raise ArtifactHashMismatchError(
                f"contract digest mismatch for template version {version_id}"
            )
        return contract

    def load_docx(self, version_id: TemplateVersionId) -> bytes:
        """Return the template DOCX bytes after verifying ``docx_sha256``."""
        row = self._require_row(version_id)
        manifest = self._manifest(row)
        expected = manifest["files"][DOCX_FILE]
        if expected != row.docx_sha256:
            raise ArtifactHashMismatchError(
                f"manifest docx digest disagrees with the ledger for {version_id}"
            )
        return self._store.read_verified(
            self._store.template_relative(row.template_id, str(row.version), DOCX_FILE),
            expected,
        )

    # ------------------------------------------------------------- internals

    def _next_version(self, template_id: str) -> int:
        """max(version) + 1 via a read-only query (single-writer ledger)."""
        cursor = self._ledger.connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS max_version FROM template_versions"
            " WHERE template_id = ?",
            (template_id,),
        )
        row = cursor.fetchone()
        current = 0 if row is None else int(row["max_version"])
        return current + 1

    def _require_row(self, version_id: TemplateVersionId) -> TemplateVersionRow:
        row = self._ledger.template_versions.get(version_id)
        if row is None:
            raise ArtifactNotFoundError(f"template version {version_id} not registered")
        return row

    def _manifest(self, row: TemplateVersionRow) -> dict[str, Any]:
        raw = self._store.read(
            self._store.template_relative(row.template_id, str(row.version), MANIFEST_FILE)
        )
        try:
            manifest = cast(dict[str, Any], json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ArtifactHashMismatchError(
                f"manifest of template version {row.template_version_id} is unreadable"
            ) from exc
        _cross_check(manifest, row)
        return manifest


def _cross_check(manifest: dict[str, Any], row: TemplateVersionRow) -> None:
    """Manifest must agree with the ledger row on every recorded digest."""
    pairs = (
        ("docx_sha256", row.docx_sha256),
        ("contract_sha256", row.contract_sha256),
        ("styles_sha256", row.styles_sha256),
        ("static_map_sha256", row.static_map_sha256),
    )
    for key, expected in pairs:
        if manifest.get(key) != expected:
            raise ArtifactHashMismatchError(
                f"manifest {key} disagrees with the ledger row for {row.template_version_id}"
            )
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ArtifactHashMismatchError(
            f"manifest of {row.template_version_id} lacks a files digest map"
        )
    for name in _TEMPLATE_FILES:
        if name not in files:
            raise ArtifactHashMismatchError(
                f"manifest of {row.template_version_id} lacks digest for {name}"
            )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
