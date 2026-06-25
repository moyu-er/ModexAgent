"""LLM provider configuration."""

from __future__ import annotations

from pydantic import BaseModel


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
