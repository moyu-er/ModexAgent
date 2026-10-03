# app/models/registry.py
"""Bot multi-provider/multi-model configuration parsing (the models: block of config/model.yml)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modex_agent.app.config_domain import Secret
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.llm_request import ReasoningEffort
from modex_agent.providers.llm_config import LLMConfig, Modality, ModelCapabilities
from modex_agent.providers.protocol_engines import OPENAI_COMPATIBLE_FORMAT

# Tokens reserved for the input (prompt+history) when synthesize_llm_config
# clamps the output budget (PRD per-model-context-compaction §4.2):
# max_output_tokens ≤ context_limit − this value.
DEFAULT_OUTPUT_RESERVE_TOKENS = 4096


class ModelCfg(BaseModel):
    """User-visible configuration for a single model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    model: str
    capabilities: list[Modality] = Field(default_factory=lambda: [Modality.TEXT])
    temperature: float = 0.7
    top_p: float | None = None
    # ge=1 aligns with ModelInfo.max_output_tokens: a model.yml writing
    # 0/negative would trigger a ValidationError in resolved.model_info (when
    # ModelInfo is bound), so the parsing face rejects it up front.
    max_output_tokens: int = Field(default=50000, ge=1)
    # None = undeclared, inherits the global max_context_tokens (PRD §4.2 D1).
    context_limit: int | None = Field(
        default=None,
        ge=1,
        description="Context-window ceiling (tokens) for this model; None inherits the global max_context_tokens",
    )
    reasoning_effort: ReasoningEffort = ReasoningEffort.NONE

    @field_validator("capabilities", mode="before")
    @classmethod
    def _coerce_caps(cls, value: Any) -> Any:  # noqa: ANN401  pre-coercion raw YAML input
        if value is None:
            return [Modality.TEXT]
        if isinstance(value, list | tuple):
            return [Modality(m) for m in value]
        return value


class ProviderCfg(BaseModel):
    """One provider and the models under it."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    key: str
    name: str
    base_url: str = ""
    interface_format: str = OPENAI_COMPATIBLE_FORMAT
    api_key: Annotated[str, Secret()]
    headers: dict[str, str] = Field(default_factory=dict)
    # Defaults to False: third-party Responses endpoints widely reject
    # store=true (ADR-0046 flip condition (c)); store=false +
    # encrypted_content replay is the universal path.
    responses_store: bool = False
    # Full URL override: when non-empty, bypasses per-format URL construction
    # from base_url (provider-level — the endpoint belongs to the provider).
    endpoint_url: str = ""
    # Optional override for the model-list endpoint. When set, the model-fetch
    # service uses this URL verbatim instead of auto-constructing candidates
    # from base_url. Leave empty for standard OpenAI-compatible /v1/models.
    models_url: str | None = None
    models: list[ModelCfg] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate(cls, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            return data

        if "url" in data and "base_url" not in data:
            data = {**data, "base_url": data["url"]}
        data = {k: v for k, v in data.items() if k != "url"}

        return data


class ResolvedModel(BaseModel):
    """Immutable value object resolved from (provider, model); read-only within a turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ProviderCfg
    model: ModelCfg

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(modalities=frozenset(self.model.capabilities))

    @property
    def model_info(self) -> ModelInfo:
        """Framework ModelInfo value object for this model (with context_limit/max_output_tokens budget profile).

        ModelChoiceBindHook (per-turn overwrite) and pool assembly (static
        default) share this single mapping.
        """
        return ModelInfo(
            model_name=self.model.model,
            capabilities=self.capabilities,
            context_limit=self.model.context_limit,
            max_output_tokens=self.model.max_output_tokens,
        )


class ModelRegistry(BaseModel):
    """The single parsed form of config/model.yml's models: block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_provider: str
    default_model: str
    # Semantics (post PRD §4.2 D1): the ceiling fallback for models that
    # do not declare context_limit — models that declare context_limit use
    # their own window and never look at this value.
    max_context_tokens: int = Field(
        default=200000,
        description="Context-ceiling fallback (tokens) for models that do not declare context_limit",
    )
    providers: list[ProviderCfg] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> ModelRegistry:
        pnames = [p.name for p in self.providers]
        if len(set(pnames)) != len(pnames):
            raise ValueError("duplicate provider.name in models config")
        pkeys = [p.key for p in self.providers]
        if len(set(pkeys)) != len(pkeys):
            raise ValueError("duplicate provider.key in models config")
        seen: set[tuple[str, str]] = set()
        for p in self.providers:
            for m in p.models:
                key = (p.name, m.name)
                if key in seen:
                    raise ValueError(f"duplicate (provider.name, model.name): {key}")
                seen.add(key)
        if self.resolve(self.default_provider, self.default_model) is None:
            raise ValueError(
                f"default_provider/default_model ({self.default_provider!r},"
                f" {self.default_model!r}) not found in config"
            )
        return self

    @classmethod
    def _extract_models_block(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Return the models config block, accepting either flat or legacy `models:` framing."""
        if "models" in data and isinstance(data["models"], dict):
            return data["models"]
        return data

    @classmethod
    def from_yaml(cls, path: Path) -> ModelRegistry:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(cls._extract_models_block(data))

    def find_provider(self, name: str) -> ProviderCfg | None:
        return next((p for p in self.providers if p.name == name), None)

    def find_provider_by_key(self, key: str) -> ProviderCfg | None:
        return next((p for p in self.providers if p.key == key), None)

    def resolve(self, provider_name: str | None, model_name: str | None) -> ResolvedModel | None:
        if not provider_name or not model_name:
            return None
        p = self.find_provider(provider_name)
        if p is None:
            return None
        m = next((x for x in p.models if x.name == model_name), None)
        if m is None:
            return None
        return ResolvedModel(provider=p, model=m)

    def default_resolved(self) -> ResolvedModel:
        r = self.resolve(self.default_provider, self.default_model)
        assert r is not None, "default model validated at construction"
        return r

    def all_choices(self) -> list[tuple[str, str, int | None]]:
        """Every (provider.name, model.name, context_limit) choice.

        ``context_limit`` is the model's declared window ceiling (``None``
        = inherit the global ``max_context_tokens``) — the selector badge
        and budget math consume it via ``/api/models``.
        """
        return [
            (p.name, m.name, m.context_limit)
            for p in self.providers
            for m in p.models
        ]

    def synthesize_llm_config(self, resolved: ResolvedModel | None = None) -> LLMConfig:
        """Synthesize the framework LLMConfig for a model (used by the runtime to build an LLMProvider)."""
        r = resolved or self.default_resolved()
        max_output_tokens = r.model.max_output_tokens
        if r.model.context_limit is not None:
            # Clamp: the output budget must not crowd out input space
            # (context_limit − reserve); under a tiny limit the reserve fills
            # the window and the ceiling degrades to 1, keeping the
            # synthesized result always positive.
            ceiling = max(r.model.context_limit - DEFAULT_OUTPUT_RESERVE_TOKENS, 1)
            max_output_tokens = min(max_output_tokens, ceiling)
        return LLMConfig(
            model=r.model.model,
            api_key=r.provider.api_key,
            base_url=r.provider.base_url,
            headers=r.provider.headers,
            responses_store=r.provider.responses_store,
            endpoint_url=r.provider.endpoint_url,
            temperature=r.model.temperature,
            top_p=r.model.top_p if r.model.top_p is not None else 0.95,
            max_output_tokens=max_output_tokens,
            capabilities=r.capabilities,
            reasoning_effort=r.model.reasoning_effort,
            interface_format=r.provider.interface_format,
        )


def placeholder_model_registry() -> ModelRegistry:
    """A minimal valid ModelRegistry used when no model.yml is configured.

    Lets the bot boot so the user can configure a real model via the WebUI
    (Settings -> Models) or ``modexbot config``. The placeholder provider has
    empty api_key/base_url, so every real LLM call fails — but
    ``ModelSelectionProvider.stream`` fails fast with a ``StreamFailure`` terminal
    event (folded into an ``LLMResponse(finish_reason=ERROR)``), and the
    ReAct LLM/end nodes surface that as a turn error instead of crashing
    the process.
    """
    return ModelRegistry(
        default_provider="_unconfigured",
        default_model="_placeholder",
        providers=[
            ProviderCfg(
                key="_unconfigured",
                name="_unconfigured",
                api_key="",
                base_url="",
                models=[
                    ModelCfg(name="_placeholder", model="_placeholder"),
                ],
            )
        ],
    )


def resolved_or_placeholder(cfg: ModelRegistry | None) -> ModelRegistry:
    """Return ``cfg`` when a real model is configured, else the placeholder."""
    return cfg or placeholder_model_registry()
