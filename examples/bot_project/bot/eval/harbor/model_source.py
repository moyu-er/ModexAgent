"""Host-side model settings resolution for the Harbor smoke gate.

Resolves the dispatch model string, credentials, and sampling parameters from
CLI/env, falling back to the bot's ``config/model.yml`` default. Runs on the
host only — model.yml (and therefore credentials) never enters the container
source manifest.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from bot.service.model_config import BotModelConfig
from modex_agent.core.constants import InterfaceFormat, ReasoningEffort

ModelSource = Literal["cli", "env", "model-default"]

_BOT_PROJECT: Final = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_YML: Final = _BOT_PROJECT / "config" / "model.yml"
_DEFAULT_TEMPERATURE: Final = 0.7
_DEFAULT_REASONING_EFFORT: Final = ReasoningEffort.NONE
# Mirrors the BotModelConfig.max_context_tokens field default.
_DEFAULT_MAX_CONTEXT_TOKENS: Final = 200000
# Mirrors the ModelCfg.max_output_tokens field default.
_DEFAULT_MAX_OUTPUT_TOKENS: Final = 50000


class ModelSourceError(RuntimeError):
    """No usable model settings could be resolved for dispatch."""


class ResolvedModelSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    api_key: str | None = None
    base_url: str | None = None
    temperature: float
    reasoning_effort: ReasoningEffort
    max_context_tokens: int
    max_output_tokens: int
    source: ModelSource


class _YmlModelDefaults(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    api_key: str | None
    base_url: str | None
    temperature: float
    reasoning_effort: ReasoningEffort
    max_context_tokens: int
    max_output_tokens: int


def _load_yml_defaults(path: Path) -> tuple[_YmlModelDefaults | None, str | None]:
    if not path.is_file():
        return None, f"model.yml not found: {path}"
    try:
        config = BotModelConfig.from_yaml(path)
        llm = config.synthesize_llm_config()
    except (OSError, ValueError, yaml.YAMLError) as error:
        return None, f"model.yml unusable: {error}"
    if not llm.api_key and not llm.base_url:
        return None, f"model.yml default has no api_key/base_url: {path}"
    prefix = (
        "anthropic"
        if llm.interface_format is InterfaceFormat.ANTHROPIC
        else "openai"
    )
    return (
        _YmlModelDefaults(
            model=f"{prefix}/{llm.model}",
            api_key=llm.api_key or None,
            base_url=llm.base_url or None,
            temperature=llm.temperature,
            reasoning_effort=llm.reasoning_effort,
            max_context_tokens=config.max_context_tokens,
            max_output_tokens=llm.max_output_tokens,
        ),
        None,
    )


def _resolve_temperature(env: Mapping[str, str], yml: _YmlModelDefaults | None) -> float:
    raw = env.get("MODEX_TEMPERATURE")
    if not raw:
        return yml.temperature if yml is not None else _DEFAULT_TEMPERATURE
    try:
        return float(raw)
    except ValueError as error:
        raise ModelSourceError(f"MODEX_TEMPERATURE={raw!r} is not a number") from error


def _resolve_reasoning_effort(
    env: Mapping[str, str],
    yml: _YmlModelDefaults | None,
) -> ReasoningEffort:
    raw = env.get("MODEX_REASONING_EFFORT")
    if not raw:
        return yml.reasoning_effort if yml is not None else _DEFAULT_REASONING_EFFORT
    try:
        return ReasoningEffort(raw)
    except ValueError as error:
        raise ModelSourceError(
            f"MODEX_REASONING_EFFORT={raw!r} is not one of "
            f"{', '.join(effort.value for effort in ReasoningEffort)}"
        ) from error


def resolve_model_settings(
    cli_model: str | None = None,
    model_yml: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ResolvedModelSettings:
    """Resolve model/credentials/parameters: CLI > env > model.yml default."""
    env = os.environ if environ is None else environ
    yml, yml_reason = _load_yml_defaults(model_yml or DEFAULT_MODEL_YML)

    model: str
    source: ModelSource
    if cli_model:
        model, source = cli_model, "cli"
    elif env_model := env.get("LLM_MODEL"):
        model, source = env_model, "env"
    elif yml is not None:
        model, source = yml.model, "model-default"
    else:
        raise ModelSourceError(f"no CLI/env model and {yml_reason}")

    return ResolvedModelSettings(
        model=model,
        api_key=env.get("LLM_API_KEY") or (yml.api_key if yml is not None else None),
        base_url=env.get("LLM_BASE_URL") or (yml.base_url if yml is not None else None),
        temperature=_resolve_temperature(env, yml),
        reasoning_effort=_resolve_reasoning_effort(env, yml),
        max_context_tokens=(
            yml.max_context_tokens if yml is not None else _DEFAULT_MAX_CONTEXT_TOKENS
        ),
        max_output_tokens=(
            yml.max_output_tokens if yml is not None else _DEFAULT_MAX_OUTPUT_TOKENS
        ),
        source=source,
    )


def inject_model_env(
    settings: ResolvedModelSettings,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Fill absent model env slots (env-wins: existing values are never clobbered)."""
    target = os.environ if environ is None else environ
    values = {
        "LLM_MODEL": settings.model,
        "LLM_API_KEY": settings.api_key,
        "LLM_BASE_URL": settings.base_url,
        "MODEX_TEMPERATURE": str(settings.temperature),
        "MODEX_REASONING_EFFORT": settings.reasoning_effort.value,
        "MODEX_MAX_CONTEXT_TOKENS": str(settings.max_context_tokens),
        "MODEX_MAX_OUTPUT_TOKENS": str(settings.max_output_tokens),
    }
    for name, value in values.items():
        if value is not None:
            target.setdefault(name, value)


__all__ = [
    "DEFAULT_MODEL_YML",
    "ModelSource",
    "ModelSourceError",
    "ResolvedModelSettings",
    "inject_model_env",
    "resolve_model_settings",
]
