"""Model configuration (models.toml, D1: TOML over YAML).

Secrets never live here: ``api_key_env`` names an environment variable, and
the key is read at call time only — never persisted to the ledger, artifacts
or logs (v1.1 §1.1).

    # models.toml
    [model]
    provider = "anthropic"            # Anthropic wire format
    model_id = "claude-..."
    base_url = "https://your-endpoint" # omit for the official API
    api_key_env = "ANTHROPIC_API_KEY"

    [search]                           # optional; absent → web search disabled
    provider = "tavily"                # tavily | searxng | generic
    base_url = "https://api.tavily.com" # searxng: instance address; generic: custom JSON endpoint
    api_key_env = "TAVILY_API_KEY"     # optional (searxng usually runs without a key)
    max_results = 5
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    base_url: str | None = None
    api_key_env: str | None = None
    call_timeout_s: int = Field(default=120, ge=1)


class SearchSettings(BaseModel):
    """Optional ``[search]`` section: web-search backend configuration.

    Absent section → ``ModelsConfig.search is None`` → search disabled. Every
    endpoint still has to pass ``ModelGateway.guard_web_endpoint`` at call
    time (``local_only`` accepts loopback instances such as a local SearXNG
    only); this config never grants egress by itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str | None = None
    max_results: int = Field(default=5, ge=1, le=10)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: ModelSettings | None = None
    search: SearchSettings | None = None

    def endpoint_config(self) -> ModelSettings | None:
        return self.model


def load_models_config(path: Path) -> ModelsConfig:
    """Load models.toml; a missing file yields an empty (offline) config."""
    if not path.exists():
        return ModelsConfig()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return ModelsConfig.model_validate(data)


def default_models_path(home: Path) -> Path:
    return home / "models.toml"
