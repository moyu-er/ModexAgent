"""LLM provider configuration."""

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
