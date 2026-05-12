"""LLM provider configuration."""

from pydantic import BaseModel


class LLMConfig(BaseModel):
    """LLM provider configuration with sensible defaults.

    All fields have defaults so users only need to set model + api_key.
    """

    provider: str = "openai"
    model: str = "gpt-4"
    api_key: str = ""
    api_base: str = ""
    temperature: float = 0.7
    max_tokens: int = 80000
