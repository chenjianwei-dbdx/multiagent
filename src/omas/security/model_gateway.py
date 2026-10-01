"""Unified model gateway: the policy chokepoint for all LLM traffic (v1.1 §6).

The gateway implements Master §18 / v1.1 §6 data-policy enforcement:

- default policy is ``local_only``: no cloud provider may be constructed, no
  request may leave the machine, and local endpoints must point at a host in
  the administrator-configured loopback allowlist
- ``llm_allowed`` is an explicit, user-witnessed upgrade recorded through a
  user decision — never something this module grants implicitly, and never
  triggered by a model error or missing configuration
- :meth:`ModelGateway.guard_request` re-checks the endpoint immediately
  before every request and raises :class:`ModelGatewayError` while the
  process is still fully offline — rejection happens **before** any network
  byte is sent, and callers must not open a transport, retry or fall back
  afterwards (missing compliant model → awaiting_user, policy unchanged)

The gateway performs policy decisions only — it is deliberately not an HTTP
client. Agent construction (a later task) must call ``check_endpoint`` when
building the provider and ``guard_request`` on every request.

Telemetry: external tracing/telemetry (logfire or similar OTel exporters)
must stay OFF on the gateway path; see :data:`EXTERNAL_TELEMETRY_DEFAULT_OFF`.

Payload policy note: ``guard_request`` inspects the *endpoint*, never the
payload. Whether task material may be sent to an endpoint at all is decided
by the task's data policy (endpoint allow/deny) — material text arriving as
payload is data, not an instruction, and is never parsed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlparse

from omas.domain.errors import ModelGatewayError
from omas.domain.ids import ArtifactId, TaskId
from omas.domain.task import DataPolicy
from omas.storage.db import Ledger

EXTERNAL_TELEMETRY_DEFAULT_OFF: Final[bool] = True
"""External tracing/telemetry is disabled by default and must stay so.

Enabling any external tracer (e.g. logfire) on the gateway path would leak
prompts, snippets, file names and error text to a third party — exactly what
``local_only`` forbids. Do not wire a tracer into this module or its callers.
"""

#: Cloud provider family rejected outright under ``local_only``, regardless
#: of any base_url a configuration might carry.
CLOUD_PROVIDERS: Final[frozenset[str]] = frozenset(
    {
        "openai",
        "anthropic",
        "google",
        "bedrock",
        "azure",
        "groq",
        "mistral",
        "cohere",
    }
)

_HTTP_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Endpoint categories a web egress URL may be classified under
#: (search-provider calls vs. direct page fetches).
WEB_CATEGORIES: Final[frozenset[str]] = frozenset({"web_search", "web_fetch"})

#: Loopback hosts a local endpoint may target (lowercase comparison).
DEFAULT_LOOPBACK_ALLOWLIST: Final[tuple[str, ...]] = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True, slots=True)
class ModelEndpointConfig:
    """One model endpoint configuration under gateway review.

    ``provider`` is the family name ("ollama", "llama_cpp", "vllm", ... or
    "test" for offline test doubles). ``base_url`` is required for local
    providers and must parse to a loopback host from the allowlist; any port
    is acceptable, embedded credentials are not. ``api_key_env`` names an
    environment variable (never a literal key).
    """

    provider: str
    model_id: str
    base_url: str | None = None
    api_key_env: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Outcome of an endpoint review: allow/deny plus an auditable reason.

    Reasons name the provider/host/policy only — never payload content,
    prompt text or secret material.
    """

    allowed: bool
    reason: str


class ModelGateway:
    """Policy gate every model endpoint and request must pass.

    Construct once with the task's (or installation's) data policy and the
    administrator-configured loopback allowlist; call ``check_endpoint``
    before building any provider client and ``guard_request`` before every
    request. Rejection is an error before any network I/O, not a fallback.
    """

    def __init__(
        self,
        policy: DataPolicy = DataPolicy.LOCAL_ONLY,
        loopback_allowlist: tuple[str, ...] = DEFAULT_LOOPBACK_ALLOWLIST,
    ) -> None:
        self._policy = policy
        self._allowlist = tuple(host.lower() for host in loopback_allowlist)

    @property
    def policy(self) -> DataPolicy:
        return self._policy

    @property
    def loopback_allowlist(self) -> tuple[str, ...]:
        return self._allowlist

    def check_endpoint(self, config: ModelEndpointConfig) -> PolicyDecision:
        """Decide whether *config* may be used under the active policy.

        local_only rules (v1.1 §6):

        - ``provider == "test"`` is allowed (offline test double, no network)
        - any cloud provider in :data:`CLOUD_PROVIDERS` is rejected, no
          matter what ``base_url`` claims
        - every other (local) provider must carry an http(s) ``base_url``
          whose host is in the loopback allowlist; URLs with embedded user
          credentials, non-http(s) schemes or no host are rejected; the port
          is unconstrained

        ``llm_allowed`` permits cloud providers and non-loopback hosts, but
        the decision reason states that explicitly so audits can trace the
        user-granted policy. URL shape violations (bad scheme, missing host,
        embedded credentials, unparseable URL) are rejected under both
        policies — they are malformed configurations, not policy questions.
        """
        provider = config.provider.strip().lower()
        if provider == "test":
            return PolicyDecision(True, "provider 'test' is an offline double; no network I/O")
        if provider in CLOUD_PROVIDERS:
            if self._policy is DataPolicy.LOCAL_ONLY:
                return PolicyDecision(
                    False,
                    f"cloud provider {config.provider!r} is denied under local_only "
                    "(no implicit fallback to or through cloud models)",
                )
            return PolicyDecision(
                True,
                f"cloud provider {config.provider!r} permitted by explicit llm_allowed policy",
            )
        if config.base_url is None:
            return PolicyDecision(
                False,
                f"local provider {config.provider!r} requires an explicit base_url",
            )
        try:
            parsed = urlparse(config.base_url)
            scheme = parsed.scheme.lower()
            host = (parsed.hostname or "").lower()
            has_credentials = parsed.username is not None or parsed.password is not None
        except ValueError as exc:
            return PolicyDecision(False, f"malformed base_url for {config.provider!r}: {exc}")
        if scheme not in _HTTP_SCHEMES:
            return PolicyDecision(
                False,
                f"base_url scheme {scheme!r} is not http/https (provider {config.provider!r})",
            )
        if not host:
            return PolicyDecision(
                False,
                f"base_url has no host component (provider {config.provider!r})",
            )
        if has_credentials:
            return PolicyDecision(
                False,
                "base_url must not embed user credentials",
            )
        if host not in self._allowlist:
            if self._policy is DataPolicy.LOCAL_ONLY:
                return PolicyDecision(
                    False,
                    f"base_url host {host!r} is not in the loopback allowlist "
                    "(local_only accepts administrator-configured loopback hosts only)",
                )
            return PolicyDecision(
                True,
                f"non-loopback host {host!r} permitted by explicit llm_allowed policy",
            )
        return PolicyDecision(
            True,
            f"local endpoint host {host!r} is loopback under policy {self._policy.value}",
        )

    def guard_request(
        self, config: ModelEndpointConfig, payload_summary: Mapping[str, object]
    ) -> None:
        """Re-check *config* immediately before one request.

        Raises :class:`ModelGatewayError` (before any network byte is sent)
        when the endpoint is denied; returns ``None`` when the request may
        proceed. ``payload_summary`` is accepted for call-site traceability
        but is deliberately **not inspected**: policy looks at endpoints,
        never at content — material in a payload is data.
        """
        decision = self.check_endpoint(config)
        if not decision.allowed:
            raise ModelGatewayError(decision.reason)

    def check_web_endpoint(self, base_url: str, *, category: str) -> PolicyDecision:
        """Decide whether a web egress *base_url* may be used under the policy.

        Separate from :meth:`check_endpoint` (model providers): web endpoints
        have no provider family, and — unlike model endpoints — a loopback
        host is acceptable under ``local_only`` too, because a local search
        instance (e.g. a self-hosted SearXNG) keeps every byte on the machine.

        - ``local_only``: non-loopback hosts are denied — 联网能力需要
          llm_allowed (web access is a cloud capability, never implicit)
        - ``llm_allowed``: any http(s) host is permitted and the decision
          reason records the endpoint *category* for audit
        - URL shape violations (non-http(s) scheme, missing host, embedded
          credentials, unparseable URL) are rejected under both policies,
          exactly like :meth:`check_endpoint`
        """
        if category not in WEB_CATEGORIES:
            raise ValueError(
                f"unknown web endpoint category {category!r}; "
                f"expected one of {sorted(WEB_CATEGORIES)}"
            )
        try:
            parsed = urlparse(base_url)
            scheme = parsed.scheme.lower()
            host = (parsed.hostname or "").lower()
            has_credentials = parsed.username is not None or parsed.password is not None
        except ValueError as exc:
            return PolicyDecision(
                False, f"malformed web endpoint URL (category {category!r}): {exc}"
            )
        if scheme not in _HTTP_SCHEMES:
            return PolicyDecision(
                False,
                f"web endpoint scheme {scheme!r} is not http/https (category {category!r})",
            )
        if not host:
            return PolicyDecision(
                False,
                f"web endpoint URL has no host component (category {category!r})",
            )
        if has_credentials:
            return PolicyDecision(
                False,
                "web endpoint URL must not embed user credentials",
            )
        if self._policy is DataPolicy.LLM_ALLOWED:
            return PolicyDecision(
                True,
                f"{category} endpoint host {host!r} permitted by explicit llm_allowed policy",
            )
        if host in self._allowlist:
            return PolicyDecision(
                True,
                f"{category} loopback host {host!r} allowed under local_only "
                "(local instance, e.g. self-hosted SearXNG; no egress)",
            )
        return PolicyDecision(
            False,
            f"{category} to non-loopback host {host!r} is denied under local_only "
            "(联网能力需要 llm_allowed)",
        )

    def guard_web_endpoint(self, base_url: str, *, category: str) -> None:
        """Re-check a web egress URL immediately before any request byte.

        Same contract as :meth:`guard_request`: raises
        :class:`ModelGatewayError` while still fully offline when the URL is
        denied; no transport may open, retry or fall back afterwards.
        """
        decision = self.check_web_endpoint(base_url, category=category)
        if not decision.allowed:
            raise ModelGatewayError(decision.reason)


def record_call(
    ledger: Ledger,
    *,
    call_id: str,
    provider: str,
    model_id: str,
    created_at: datetime,
    task_id: TaskId | None = None,
    node_name: str | None = None,
    model_revision: str | None = None,
    prompt_template_digest: str | None = None,
    system_prompt_digest: str | None = None,
    tool_schema_version: str | None = None,
    model_config_json: str | None = None,
    input_artifact_refs_json: str | None = None,
    raw_output_artifact_id: ArtifactId | None = None,
    parsed_output_artifact_id: ArtifactId | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost: float | None = None,
) -> None:
    """Thin, signature-aligned wrapper around ``Ledger.llm_calls.insert``.

    Records one LLM call's metadata: model config JSON, prompt digests, token
    usage and refs to the raw/parsed output artifacts (never the outputs
    themselves — SQLite keeps refs and finite metadata only).

    Cost semantics (v1.1 §4.3 / dependency-baseline probe 1): usage numbers
    are real values read from ``RunUsage`` (they are non-zero even for
    TestModel), and an unknown cost must be recorded as ``None`` (SQL NULL).
    A literal ``0`` is refused — local calls are free, but "free" is a fact
    the operator records deliberately, never a placeholder for "unknown".
    """
    if cost is not None and cost <= 0:
        raise ModelGatewayError(
            "cost must be None (unknown) or a positive amount; never 0 for unknown cost"
        )
    ledger.llm_calls.insert(
        call_id=call_id,
        task_id=task_id,
        node_name=node_name,
        provider=provider,
        model_id=model_id,
        model_revision=model_revision,
        prompt_template_digest=prompt_template_digest,
        system_prompt_digest=system_prompt_digest,
        tool_schema_version=tool_schema_version,
        model_config_json=model_config_json,
        input_artifact_refs_json=input_artifact_refs_json,
        raw_output_artifact_id=raw_output_artifact_id,
        parsed_output_artifact_id=parsed_output_artifact_id,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
        created_at=created_at,
    )
