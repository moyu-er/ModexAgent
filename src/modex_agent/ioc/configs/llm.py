"""LLM provider configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modex_agent.core.constants import InterfaceFormat, ReasoningEffort


class Modality(StrEnum):
    """A perceptual channel a model can accept.

    TEXT is always available for any text model; IMAGE/VIDEO/AUDIO are
    native-multimodal flags, default-off. Extensible — adding a modality is
    one enum member. See ADR-0013 §9.
    """

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


@dataclass(frozen=True)
class ModelCapabilities:
    """Frozen value object exposing the modalities a model can consume.

    Read from per-pool config (``LLMConfig.capabilities``) and gates image
    inlining (ADR-0014 §1): a pool declaring ``IMAGE`` enables mechanism A;
    the default TEXT-only leaves every attachment on the mechanism-B tool
    path. Defaults to TEXT-only.
    """

    modalities: frozenset[Modality] = field(default_factory=lambda: frozenset({Modality.TEXT}))

    def supports(self, modality: Modality) -> bool:
        """True if ``modality`` is among this model's capabilities."""
        return modality in self.modalities


class LLMConfig(BaseModel):
    """LLM provider configuration.

    The provider is inferred from the model name (e.g. openai/gpt-4).
    For OpenAI-compatible endpoints (MiniMax, DeepSeek, etc.), set base_url.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = "gpt-4"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.7
    max_output_tokens: int = 80000
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    reasoning_effort: ReasoningEffort = ReasoningEffort.NONE
    interface_format: InterfaceFormat = InterfaceFormat.OPENAI_COMPATIBLE

    @field_validator("capabilities", mode="before")
    @classmethod
    def _coerce_capabilities(
        cls, value: ModelCapabilities | list[str] | tuple[str, ...] | None
    ) -> ModelCapabilities:
        """Coerce a flat ``list[str]`` from YAML into a ``ModelCapabilities``.

        The pool YAML loader feeds parsed YAML in, so ``capabilities`` may
        arrive as ``["text", "image"]``. A ``ModelCapabilities`` is passed
        through unchanged; ``None`` falls back to the TEXT-only default.
        Unknown modality strings raise ``ValueError`` → ``ValidationError``.
        """
        if value is None:
            return ModelCapabilities()
        if isinstance(value, ModelCapabilities):
            return value
        if isinstance(value, (list, tuple)):
            modalities = frozenset(Modality(item) for item in value)
            return ModelCapabilities(modalities=modalities)
        raise ValueError(
            f"capabilities must be a list[str], tuple[str, ...], "
            f"ModelCapabilities, or None; got {type(value).__name__}"
        )

    def missing_required_fields(self) -> list[str]:
        """Return list of required fields that are empty.

        The three fields below are needed for the provider factory to create
        a working LLM connection.  When any are missing, ``BotService`` warns
        at startup and the CLI ``install`` / ``config`` commands check them.
        """
        missing: list[str] = []
        if not self.model:
            missing.append("model")
        if not self.api_key:
            missing.append("api_key")
        if not self.base_url:
            missing.append("base_url")
        return missing
