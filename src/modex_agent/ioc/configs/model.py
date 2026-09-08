"""Global model configuration.

A single, app-wide model definition (``url`` / ``api_key`` / ``model`` /
``capabilities`` plus sampling defaults) that every pool inherits unless it
declares its own ``llm`` override. This is the one source of truth for model
settings; it lives in ``config/model.yml`` and is edited through the CLI, not
through environment variables.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modex_agent.ioc.configs.llm import InterfaceFormat


class GlobalModelConfig(BaseModel):
    """App-wide model settings shared by every pool.

    ``url`` is accepted as a backward-compatible alias for ``base_url``.
    ``to_llm_dict`` returns the canonical ``LLMConfig`` shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    capabilities: list[str] = Field(default_factory=lambda: ["text"])
    temperature: float = 0.7
    max_output_tokens: int = 80000
    interface_format: InterfaceFormat = InterfaceFormat.OPENAI_COMPATIBLE
    headers: dict[str, str] = Field(default_factory=dict)
    responses_store: bool = False
    endpoint_url: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_url(cls, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data, dict) and "url" in data and "base_url" not in data:
            data = {**data, "base_url": data["url"]}
            data = {k: v for k, v in data.items() if k != "url"}
        return data

    def to_llm_dict(self) -> dict[str, Any]:
        """Return an ``LLMConfig``-shaped dict."""
        return {
            "model": self.model,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "capabilities": list(self.capabilities),
            "interface_format": self.interface_format,
            "headers": dict(self.headers),
            "responses_store": self.responses_store,
            "endpoint_url": self.endpoint_url,
        }
