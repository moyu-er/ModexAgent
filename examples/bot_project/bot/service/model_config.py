# bot/service/model_config.py
"""Bot 多 provider/多模型配置解析（config/model.yml 的 models: 块）。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bot.config.domain import Secret
from modex_agent.ioc.configs.llm import LLMConfig, Modality, ModelCapabilities

_DEFAULTS_CAPS = [Modality.TEXT]
_OPENAI_PREFIX = "openai/"
_ROUTE_SEPARATOR = "/"


def _routing_model(model: str) -> str:
    """Normalize a model string for provider routing.

    A ``provider/`` prefix is a routing directive consumed by the framework
    factory: ``openai/X`` -> OpenAIProvider with ``X`` (prefix stripped);
    any other ``provider/X`` (anthropic/, mistral/, ...) -> LiteLLMProvider
    which expects the prefix kept. A bare name with no ``/`` defaults to the
    OpenAI-compatible provider (``openai/`` prepended).
    """
    if _ROUTE_SEPARATOR in model:
        return model
    return _OPENAI_PREFIX + model


class ModelCfg(BaseModel):
    """单个模型的用户可见配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    model: str
    capabilities: list[Modality] = Field(default_factory=lambda: list(_DEFAULTS_CAPS))
    temperature: float = 0.7
    max_output_tokens: int = 50000

    @field_validator("capabilities", mode="before")
    @classmethod
    def _coerce_caps(cls, value: Any) -> Any:  # noqa: ANN401  pre-coercion raw YAML input
        if value is None:
            return list(_DEFAULTS_CAPS)
        if isinstance(value, list | tuple):
            return [Modality(m) for m in value]
        return value


class ProviderCfg(BaseModel):
    """一个 provider 及其下属模型。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    name: str
    url: str
    api_key: Annotated[str, Secret()]
    models: list[ModelCfg] = Field(default_factory=list)


class ResolvedModel(BaseModel):
    """(provider, model) 解析后的不可变值对象，turn 内只读。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ProviderCfg
    model: ModelCfg

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(modalities=frozenset(self.model.capabilities))


class BotModelConfig(BaseModel):
    """config/model.yml 的 models: 块的唯一解析形式。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_provider: str
    default_model: str
    max_context_tokens: int = 200000
    providers: list[ProviderCfg] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> BotModelConfig:
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
    def from_yaml(cls, path: Path) -> BotModelConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data.get("models", {}))

    def find_provider(self, name: str) -> ProviderCfg | None:
        return next((p for p in self.providers if p.name == name), None)

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

    def all_choices(self) -> list[tuple[str, str]]:
        return [(p.name, m.name) for p in self.providers for m in p.models]

    def synthesize_llm_config(self, resolved: ResolvedModel | None = None) -> LLMConfig:
        """用某个模型合成框架 LLMConfig（供 AppConfig 后处理回填 pool_cfg.llm）。"""
        r = resolved or self.default_resolved()
        return LLMConfig(
            model=_routing_model(r.model.model),
            api_key=r.provider.api_key,
            base_url=r.provider.url,
            temperature=r.model.temperature,
            max_output_tokens=r.model.max_output_tokens,
            capabilities=r.capabilities,
        )
