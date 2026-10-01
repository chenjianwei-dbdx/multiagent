"""Gate reports (v1.1 §7): structured pass/fail/unknown evidence.

Overall pass requires **all required checks pass**. Any required check being
``unknown`` makes the overall status ``unknown`` — which is NOT pass and must
block finalize.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import Sha256Hex
from .ids import ArtifactId, TemplateVersionId

RULE_VERSION = "1"


class GateName(StrEnum):
    PROVENANCE_PRECHECK = "provenance_precheck"  # Gate A, before render
    PROVENANCE_GATE = "provenance_gate"          # Gate B, after render
    FORMAT_GATE = "format_gate"


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    status: GateStatus
    severity: Severity = Severity.BLOCKER
    #: optional checks never block finalize regardless of status
    required: bool = True
    locator: str | None = None
    #: short expected/actual digests or counts; full evidence lives in files
    expected: str | None = None
    actual: str | None = None
    evidence_artifact_id: ArtifactId | None = None


class GateReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: GateName
    #: subject hashes — a report is only reusable for the exact same subject
    #: (candidate sha + IR sha + template version + rule version)
    candidate_sha256: Sha256Hex | None = None
    render_ir_sha256: Sha256Hex
    template_version_id: TemplateVersionId
    rule_version: str = RULE_VERSION
    results: tuple[CheckResult, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_subject(self) -> GateReport:
        needs_candidate = self.gate in (GateName.PROVENANCE_GATE, GateName.FORMAT_GATE)
        if needs_candidate and self.candidate_sha256 is None:
            raise ValueError(f"{self.gate} requires candidate_sha256")
        return self

    def overall_status(self) -> GateStatus:
        required = [r for r in self.results if r.required]
        if any(r.status is GateStatus.FAIL for r in required):
            return GateStatus.FAIL
        if any(r.status is GateStatus.UNKNOWN for r in required):
            return GateStatus.UNKNOWN
        return GateStatus.PASS

    def is_finalizable(self) -> bool:
        return self.overall_status() is GateStatus.PASS

    def failed_checks(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is GateStatus.FAIL)
