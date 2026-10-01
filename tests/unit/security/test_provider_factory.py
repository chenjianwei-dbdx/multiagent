"""Provider factory: gateway-gated construction, secrets from env only."""

from __future__ import annotations

import pytest

from omas.config.settings import ModelSettings
from omas.domain.errors import ModelGatewayError
from omas.domain.task import DataPolicy
from omas.security.model_gateway import ModelGateway
from omas.security.provider_factory import build_model

REMOTE = ModelSettings(
    provider="anthropic",
    model_id="claude-sonnet-4-5",
    base_url="https://relay.example.com",
    api_key_env="OMAS_TEST_KEY",
)


def test_remote_requires_llm_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMAS_TEST_KEY", "sk-test")
    gateway = ModelGateway(policy=DataPolicy.LOCAL_ONLY)
    with pytest.raises(ModelGatewayError, match="llm_allowed"):
        build_model(REMOTE, gateway=gateway, policy=DataPolicy.LOCAL_ONLY)


def test_remote_builds_under_llm_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMAS_TEST_KEY", "sk-test")
    gateway = ModelGateway(policy=DataPolicy.LLM_ALLOWED)
    model = build_model(REMOTE, gateway=gateway, policy=DataPolicy.LLM_ALLOWED)
    assert model is not None  # constructed client; no request is made here


def test_missing_api_key_rejected_before_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMAS_TEST_KEY", raising=False)
    gateway = ModelGateway(policy=DataPolicy.LLM_ALLOWED)
    with pytest.raises(ModelGatewayError, match="OMAS_TEST_KEY"):
        build_model(REMOTE, gateway=gateway, policy=DataPolicy.LLM_ALLOWED)


def test_unsupported_provider_rejected() -> None:
    gateway = ModelGateway(policy=DataPolicy.LLM_ALLOWED)
    settings = ModelSettings(provider="openai", model_id="gpt-x", base_url="https://x")
    with pytest.raises(ModelGatewayError, match="Anthropic-format"):
        build_model(settings, gateway=gateway, policy=DataPolicy.LLM_ALLOWED)
