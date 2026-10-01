"""Provider factory: config → guarded pydantic-ai model (no silent egress).

The gateway check runs BEFORE the client is constructed, so a ``local_only``
task with a remote endpoint configured fails closed without a single network
byte leaving the machine (T12). API keys come from the environment only.
"""

from __future__ import annotations

import os

from pydantic_ai.models import Model

from omas.config.settings import ModelSettings
from omas.domain.errors import ModelGatewayError
from omas.domain.task import DataPolicy
from omas.security.model_gateway import ModelEndpointConfig, ModelGateway

_ANTHROPIC_FORMAT_PROVIDERS = frozenset({"anthropic", "anthropic-compatible"})


def endpoint_of(settings: ModelSettings) -> ModelEndpointConfig:
    return ModelEndpointConfig(
        provider=settings.provider,
        model_id=settings.model_id,
        base_url=settings.base_url,
        api_key_env=settings.api_key_env,
    )


def build_model(
    settings: ModelSettings, *, gateway: ModelGateway, policy: DataPolicy
) -> Model:
    """Construct the pydantic-ai model for *settings* under *policy*.

    Raises ModelGatewayError when the policy forbids this endpoint or the
    API key is missing — before any client exists.
    """
    endpoint = endpoint_of(settings)
    decision = gateway.check_endpoint(endpoint)
    # llm_allowed is signalled through the policy itself; local_only never
    # reaches a remote construction.
    if policy is not DataPolicy.LLM_ALLOWED and settings.base_url:
        raise ModelGatewayError(
            "remote endpoints require data_policy=llm_allowed (explicit per-task choice)"
        )
    if not decision.allowed:
        raise ModelGatewayError(f"endpoint rejected: {decision.reason}")

    if settings.provider in _ANTHROPIC_FORMAT_PROVIDERS:
        api_key: str | None = None
        if settings.api_key_env:
            api_key = os.environ.get(settings.api_key_env)
            if not api_key:
                raise ModelGatewayError(
                    f"API key environment variable {settings.api_key_env} is not set"
                )
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key=api_key, base_url=settings.base_url)
        return AnthropicModel(settings.model_id, provider=provider)
    raise ModelGatewayError(
        f"unsupported provider {settings.provider!r}; "
        "MVP supports Anthropic-format endpoints only"
    )
