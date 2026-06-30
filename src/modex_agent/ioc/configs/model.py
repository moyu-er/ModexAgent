"""Global model configuration.

A single, app-wide model definition (``url`` / ``api_key`` / ``model`` /
``capabilities`` plus sampling defaults) that every pool inherits unless it
declares its own ``llm`` override. This is the one source of truth for model
settings; it lives in ``config/model.yml`` and is edited through the CLI, not
through environment variables.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GlobalModelConfig(BaseModel):
    """App-wide model settings shared by every pool.

    Field names mirror :class:`LLMConfig` except ``url``, which maps to
    ``LLMConfig.base_url``. ``to_llm_dict`` performs that single rename so the
    result can be fed straight into ``LLMConfig`` (or merged into a pool's
    ``llm`` section before validation).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = ""
    api_key: str = ""
    model: str = ""
    capabilities: list[str] = Field(default_factory=lambda: ["text"])
    temperature: float = 0.7
    max_tokens: int = 80000

    def to_llm_dict(self) -> dict[str, Any]:
        """Return an ``LLMConfig``-shaped dict (``url`` renamed to ``base_url``)."""
        return {
            "model": self.model,
            "api_key": self.api_key,
            "base_url": self.url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "capabilities": list(self.capabilities),
        }
