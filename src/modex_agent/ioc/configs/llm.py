"""LLM provider configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.constants import InterfaceFormat, ReasoningEffort

__all__ = ["Modality", "ModelCapabilities", "ModelInfo", "LLMConfig"]


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
    top_p: float = 0.95
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
        if isinstance(value, list | tuple):
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
