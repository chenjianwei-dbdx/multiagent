"""models.toml loading: offline default, validation, secret handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omas.config.settings import default_models_path, load_models_config


def test_missing_file_yields_offline(tmp_path: Path) -> None:
    config = load_models_config(tmp_path / "models.toml")
    assert config.model is None


def test_valid_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        "[model]\n"
        'provider = "anthropic"\n'
        'model_id = "claude-sonnet-4-5"\n'
        'base_url = "https://relay.example.com/v1"\n'
        'api_key_env = "ANTHROPIC_API_KEY"\n',
        encoding="utf-8",
    )
    config = load_models_config(path)
    assert config.model is not None
    assert config.model.provider == "anthropic"
    assert config.model.api_key_env == "ANTHROPIC_API_KEY"
    assert config.endpoint_config() is config.model


def test_extra_fields_rejected(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        '[model]\nprovider = "anthropic"\nmodel_id = "m"\napi_key = "sk-secret"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_models_config(path)


def test_default_path_under_home(tmp_path: Path) -> None:
    assert default_models_path(tmp_path) == tmp_path / "models.toml"
