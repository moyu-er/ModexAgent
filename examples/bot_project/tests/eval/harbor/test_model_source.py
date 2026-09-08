from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from bot.eval.harbor.entry import EntryConfig
from bot.eval.harbor.model_source import (
    ModelSourceError,
    ResolvedModelSettings,
    inject_model_env,
    resolve_model_settings,
)
from bot.eval.harbor.pool_mode_types import build_model_config

from modex_agent.core.llm_request import ReasoningEffort
from modex_agent.ioc.configs.llm import InterfaceFormat

_MODEL_ENV_NAMES = (
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "MODEX_TEMPERATURE",
    "MODEX_REASONING_EFFORT",
    "MODEX_MAX_CONTEXT_TOKENS",
    "MODEX_MAX_OUTPUT_TOKENS",
)


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _MODEL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _model_yml_text(
    *,
    interface_format: str | None = None,
    api_key: str = "yml-key",
    base_url: str = "https://api.stepfun.com/step_plan/v1",
    temperature: float = 0.3,
    reasoning_effort: str = "low",
    max_context_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> str:
    model_entry: dict[str, Any] = {
        "name": "yml-model",
        "model": "yml-model",
        "temperature": temperature,
        "reasoning_effort": reasoning_effort,
    }
    if max_output_tokens is not None:
        model_entry["max_output_tokens"] = max_output_tokens
    provider: dict[str, Any] = {
        "key": "provider",
        "name": "provider",
        "base_url": base_url,
        "api_key": api_key,
        "models": [model_entry],
    }
    if interface_format is not None:
        provider["interface_format"] = interface_format
    data: dict[str, Any] = {
        "default_provider": "provider",
        "default_model": "yml-model",
        "providers": [provider],
    }
    if max_context_tokens is not None:
        data["max_context_tokens"] = max_context_tokens
    return yaml.safe_dump(data)


def _write_model_yml(path: Path, **overrides: Any) -> Path:
    path.write_text(_model_yml_text(**overrides), encoding="utf-8")
    return path


def test_resolution_precedence_cli_beats_env_beats_model_yml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "openai/env-model")
    yml = _write_model_yml(tmp_path / "model.yml")

    from_cli = resolve_model_settings("openai/cli-model", yml)
    from_env = resolve_model_settings(None, yml)

    assert from_cli.model == "openai/cli-model"
    assert from_cli.source == "cli"
    assert from_env.model == "openai/env-model"
    assert from_env.source == "env"

    monkeypatch.delenv("LLM_MODEL")
    from_yml = resolve_model_settings(None, yml)

    assert from_yml.model == "openai/yml-model"
    assert from_yml.source == "model-default"
    assert from_yml.api_key == "yml-key"
    assert from_yml.base_url == "https://api.stepfun.com/step_plan/v1"
    assert from_yml.temperature == 0.3
    assert from_yml.reasoning_effort is ReasoningEffort.LOW


def test_resolution_env_key_url_and_parameters_win_over_yml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://env.example/v1")
    monkeypatch.setenv("MODEX_TEMPERATURE", "1.0")
    monkeypatch.setenv("MODEX_REASONING_EFFORT", "high")
    yml = _write_model_yml(tmp_path / "model.yml")

    settings = resolve_model_settings(None, yml)

    assert settings.model == "openai/yml-model"
    assert settings.source == "model-default"
    assert settings.api_key == "env-key"
    assert settings.base_url == "http://env.example/v1"
    assert settings.temperature == 1.0
    assert settings.reasoning_effort is ReasoningEffort.HIGH


def test_resolution_anthropic_default_prefixes_model_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)
    yml = _write_model_yml(tmp_path / "model.yml", interface_format="anthropic")

    settings = resolve_model_settings(None, yml)

    assert settings.model == "anthropic/yml-model"
    entry = EntryConfig.from_environment(
        {
            "LLM_MODEL": settings.model,
            "LLM_API_KEY": settings.api_key or "",
            "LLM_BASE_URL": settings.base_url or "",
            "MODEX_MEMORY_NS": "ns",
        }
    )
    config = build_model_config(entry)
    assert config.providers[0].interface_format is InterfaceFormat.ANTHROPIC


@pytest.mark.parametrize(
    ("model_string", "expected_format"),
    [
        ("openai/yml-model", InterfaceFormat.OPENAI_COMPATIBLE),
        ("harbor/yml-model", InterfaceFormat.OPENAI_COMPATIBLE),
    ],
)
def test_build_model_config_keeps_openai_compatible_for_other_prefixes(
    model_string: str,
    expected_format: InterfaceFormat,
) -> None:
    entry = EntryConfig.from_environment(
        {"LLM_MODEL": model_string, "MODEX_MEMORY_NS": "ns"}
    )

    config = build_model_config(entry)

    assert config.providers[0].interface_format is expected_format


def test_resolution_placeholder_default_without_cli_or_env_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)
    yml = _write_model_yml(tmp_path / "model.yml", api_key="", base_url="")

    with pytest.raises(ModelSourceError, match="api_key"):
        resolve_model_settings(None, yml)


def test_resolution_missing_yml_without_cli_or_env_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)

    with pytest.raises(ModelSourceError, match="model.yml"):
        resolve_model_settings(None, tmp_path / "absent.yml")


def test_resolution_threads_yml_max_context_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)
    yml = _write_model_yml(tmp_path / "model.yml", max_context_tokens=500000)

    from_yml = resolve_model_settings(None, yml)
    from_cli = resolve_model_settings("openai/cli-model", yml)

    assert from_yml.max_context_tokens == 500000
    assert from_cli.max_context_tokens == 500000


def test_resolution_threads_yml_max_output_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)
    yml = _write_model_yml(tmp_path / "model.yml", max_output_tokens=256000)

    from_yml = resolve_model_settings(None, yml)
    from_cli = resolve_model_settings("openai/cli-model", yml)

    assert from_yml.max_output_tokens == 256000
    assert from_cli.max_output_tokens == 256000


def test_resolution_cli_model_survives_unusable_yml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)
    yml = _write_model_yml(tmp_path / "model.yml", api_key="", base_url="")

    settings = resolve_model_settings("openai/cli-model", yml)

    assert settings.model == "openai/cli-model"
    assert settings.source == "cli"
    assert settings.api_key is None
    assert settings.base_url is None
    assert settings.temperature == 0.7
    assert settings.reasoning_effort is ReasoningEffort.NONE
    assert settings.max_context_tokens == 200000
    assert settings.max_output_tokens == 50000


def test_resolution_local_endpoint_with_empty_api_key_is_usable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)
    yml = _write_model_yml(tmp_path / "model.yml", api_key="", base_url="http://localhost:8000/v1")

    settings = resolve_model_settings(None, yml)

    assert settings.api_key is None
    assert settings.base_url == "http://localhost:8000/v1"
    assert settings.model == "openai/yml-model"


def test_resolution_remote_key_without_base_url_is_usable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)
    yml = _write_model_yml(tmp_path / "model.yml", base_url="")

    settings = resolve_model_settings(None, yml)

    assert settings.api_key == "yml-key"
    assert settings.base_url is None


def test_resolution_malformed_parameter_env_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("MODEX_TEMPERATURE", "hot")
    yml = _write_model_yml(tmp_path / "model.yml")

    with pytest.raises(ModelSourceError, match="MODEX_TEMPERATURE"):
        resolve_model_settings("openai/cli-model", yml)

    monkeypatch.setenv("MODEX_TEMPERATURE", "1.0")
    monkeypatch.setenv("MODEX_REASONING_EFFORT", "turbo")

    with pytest.raises(ModelSourceError, match="MODEX_REASONING_EFFORT"):
        resolve_model_settings("openai/cli-model", yml)


def test_inject_model_env_fills_absent_and_never_clobbers() -> None:
    settings = ResolvedModelSettings(
        model="openai/yml-model",
        api_key="yml-key",
        base_url="http://localhost:8000/v1",
        temperature=1.0,
        reasoning_effort=ReasoningEffort.HIGH,
        max_context_tokens=500000,
        max_output_tokens=256000,
        source="model-default",
    )
    environ: dict[str, str] = {"LLM_API_KEY": "explicit-key"}

    inject_model_env(settings, environ)

    assert environ == {
        "LLM_API_KEY": "explicit-key",
        "LLM_MODEL": "openai/yml-model",
        "LLM_BASE_URL": "http://localhost:8000/v1",
        "MODEX_TEMPERATURE": "1.0",
        "MODEX_REASONING_EFFORT": "high",
        "MODEX_MAX_CONTEXT_TOKENS": "500000",
        "MODEX_MAX_OUTPUT_TOKENS": "256000",
    }


def test_inject_model_env_skips_absent_credentials() -> None:
    settings = ResolvedModelSettings(
        model="openai/yml-model",
        api_key=None,
        base_url=None,
        temperature=0.7,
        reasoning_effort=ReasoningEffort.NONE,
        max_context_tokens=200000,
        max_output_tokens=50000,
        source="model-default",
    )
    environ: dict[str, str] = {}

    inject_model_env(settings, environ)

    assert environ == {
        "LLM_MODEL": "openai/yml-model",
        "MODEX_TEMPERATURE": "0.7",
        "MODEX_REASONING_EFFORT": "none",
        "MODEX_MAX_CONTEXT_TOKENS": "200000",
        "MODEX_MAX_OUTPUT_TOKENS": "50000",
    }
