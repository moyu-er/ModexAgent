"""LLM provider configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field


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

    Placeholder carried on :class:`LLMConfig` but unused in v1 — nothing reads
    it to alter behavior yet. It exists so the deferred native-multimodal
    renderer (ADR-0013 §10) has a concrete switch to bind to. Defaults to
    TEXT-only, matching every provider in v1.
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

    model: str = "gpt-4"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.7
    max_tokens: int = 80000
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)

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
