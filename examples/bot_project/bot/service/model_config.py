# bot/service/model_config.py
"""Bot 多 provider/多模型配置解析（config/model.yml 的 models: 块）。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bot.config.domain import Secret
from modex_agent.core.llm_request import ReasoningEffort
from modex_agent.ioc.configs.llm import (
    InterfaceFormat,
    LLMConfig,
    Modality,
    ModelCapabilities,
)


class ModelCfg(BaseModel):
    """单个模型的用户可见配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    model: str
    capabilities: list[Modality] = Field(default_factory=lambda: [Modality.TEXT])
    temperature: float = 0.7
    top_p: float | None = None
    max_output_tokens: int = 50000
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
    """一个 provider 及其下属模型。"""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    key: str
    name: str
    base_url: str = ""
    interface_format: InterfaceFormat = InterfaceFormat.OPENAI_COMPATIBLE
    api_key: Annotated[str, Secret()]
    headers: dict[str, str] = Field(default_factory=dict)
    # 默认 False:第三方 Responses 端点普遍拒绝 store=true(ADR-0046 flip
    # condition (c));store=false + encrypted_content replay 是普适路径。
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
    def _extract_models_block(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Return the models config block, accepting either flat or legacy `models:` framing."""
        if "models" in data and isinstance(data["models"], dict):
            return data["models"]
        return data

    @classmethod
    def from_yaml(cls, path: Path) -> BotModelConfig:
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

    def all_choices(self) -> list[tuple[str, str]]:
        return [(p.name, m.name) for p in self.providers for m in p.models]

    def synthesize_llm_config(self, resolved: ResolvedModel | None = None) -> LLMConfig:
        """用某个模型合成框架 LLMConfig（供运行时构建 LLMProvider 使用）。"""
        r = resolved or self.default_resolved()
        return LLMConfig(
            model=r.model.model,
            api_key=r.provider.api_key,
            base_url=r.provider.base_url,
            headers=r.provider.headers,
            responses_store=r.provider.responses_store,
            endpoint_url=r.provider.endpoint_url,
            temperature=r.model.temperature,
            top_p=r.model.top_p if r.model.top_p is not None else 0.95,
            max_output_tokens=r.model.max_output_tokens,
            capabilities=r.capabilities,
            reasoning_effort=r.model.reasoning_effort,
            interface_format=r.provider.interface_format,
        )


def _placeholder_model_config() -> BotModelConfig:
    """A minimal valid BotModelConfig used when no model.yml is configured.

    Lets the bot boot so the user can configure a real model via the WebUI
    (Settings -> Models) or ``modexbot config``. The placeholder provider has
    empty api_key/base_url, so every real LLM call fails — but
    ``BotModelProvider.chat_stream`` catches the provider-build failure and
    returns an ``LLMResponse(finish_reason=ERROR)``, and the ReAct LLM/end
    nodes surface that as a turn error instead of crashing the process.
    """
    return BotModelConfig(
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


def _resolved_or_placeholder(cfg: BotModelConfig | None) -> BotModelConfig:
    """Return ``cfg`` when a real model is configured, else the placeholder."""
    return cfg or _placeholder_model_config()
