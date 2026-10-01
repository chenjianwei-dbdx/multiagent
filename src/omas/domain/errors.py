"""Error taxonomy with stable machine-readable codes.

Codes are persisted in events / node_runs error columns, so they must stay
stable and finite. Never embed body text, model output or stack traces in
error messages that reach the ledger.
"""

from __future__ import annotations


class OmasError(Exception):
    """Base class for all OMAS domain/storage errors."""

    code = "OMAS_ERROR"


class IdempotencyConflictError(OmasError):
    """Same idempotency key resubmitted with a different payload."""

    code = "IDEMPOTENCY_CONFLICT"


class ConcurrencyError(OmasError):
    """Optimistic/concurrent update lost (e.g. stale epoch)."""

    code = "CONCURRENCY_CONFLICT"


class SpanResolutionError(OmasError):
    code = "SPAN_INVALID"


class SpanOutOfRangeError(SpanResolutionError):
    code = "SPAN_OUT_OF_RANGE"


class SpanEmptyError(SpanResolutionError):
    code = "SPAN_EMPTY"


class SpanHashMismatchError(SpanResolutionError):
    code = "SPAN_HASH_MISMATCH"


class ArtifactNotFoundError(OmasError):
    code = "ARTIFACT_NOT_FOUND"


class ArtifactExistsError(OmasError):
    code = "ARTIFACT_EXISTS"


class ArtifactHashMismatchError(OmasError):
    code = "ARTIFACT_HASH_MISMATCH"


class ArtifactCorruptError(OmasError):
    code = "ARTIFACT_CORRUPT"


class PathEscapeError(OmasError):
    """A path attempted to leave its allowed root or crossed a symlink."""

    code = "PATH_ESCAPE"


class CapabilityError(OmasError):
    """A writer was used outside its granted scope."""

    code = "CAPABILITY_DENIED"


class MigrationError(OmasError):
    code = "MIGRATION_FAILED"


class LedgerConflictError(OmasError):
    """Unique/CHECK violation surfaced by the ledger as a domain condition."""

    code = "LEDGER_CONFLICT"


class TaskStateError(OmasError):
    """Illegal state transition or operation on a terminal task."""

    code = "TASK_STATE_INVALID"


class DecodeError(OmasError):
    code = "DECODE_INVALID"


class PlanValidationError(OmasError):
    """A content plan violates its template contract and the repair budget is spent.

    The message may only carry the violation summary (slot ids / attribute
    names), never body text or raw model output.
    """

    code = "PLAN_INVALID"


class MissingRequiredSlotsError(OmasError):
    """Required slots stay missing with no valid user omission."""

    code = "GAP_REQUIRED_MISSING"


class FinalizeRejectedError(OmasError):
    """Finalize refused: gates missing/stale, subject mismatch, or task state."""

    code = "FINALIZE_REJECTED"


class RenderError(OmasError):
    code = "RENDER_FAILED"


class GateBlockedError(OmasError):
    """A required gate did not pass; no render/finalize may proceed."""

    code = "GATE_BLOCKED"


class BudgetExceededError(OmasError):
    """A configurable tool/model budget was exhausted (v1.1 §6).

    Budget exhaustion is a stop condition, never a reason to guess, summarise
    or pad material. Messages carry counters only, never body text.
    """

    code = "BUDGET_EXCEEDED"


class ModelGatewayError(OmasError):
    """ModelGateway refused an endpoint/request before any network byte left.

    Raised by the gateway's request guard while the process is still fully
    offline; no transport may open, retry or fall back afterwards.
    """

    code = "MODEL_GATEWAY_REJECTED"
