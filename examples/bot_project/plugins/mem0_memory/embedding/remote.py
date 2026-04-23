"""Remote embedding provider — OpenAI-compatible API."""

import logging
from typing import Any

from ..config import Mem0Config
from . import EmbeddingProvider

logger = logging.getLogger(__name__)


class RemoteEmbeddingProvider(EmbeddingProvider):
    """Remote OpenAI-compatible embedding API.

    Supports any API that follows the OpenAI embeddings format
    (POST /embeddings with model + input).
    Inherits base_url / api_key from the framework's LLM provider
    unless explicitly overridden in plugin config.
    """

    def __init__(self, config: Mem0Config):
        self._config = config
        self._resolved_base_url: str | None = None
        self._resolved_api_key: str | None = None

    def check_available(self) -> list[str]:
        # No special deps beyond what mem0 provides
        return []

    async def initialize(self, **kwargs: Any) -> None:
        """Resolve base_url/api_key from framework LLM provider if not explicit."""
        llm_provider = kwargs.get("llm_provider")

        self._resolved_base_url = self._config.embedding_base_url
        if not self._resolved_base_url and llm_provider:
            self._resolved_base_url = getattr(llm_provider, "base_url", None)

        self._resolved_api_key = self._config.embedding_api_key
        if not self._resolved_api_key and llm_provider:
            self._resolved_api_key = getattr(llm_provider, "api_key", None)

        logger.info(
            "Remote embedding configured: provider=%s, model=%s, base_url=%s",
            self._config.embedding_provider,
            self._config.embedding_model,
            self._resolved_base_url or "(none)",
        )

    async def shutdown(self) -> None:
        """No-op: no persistent resources to release."""

    def get_mem0_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "provider": self._config.embedding_provider,
            "config": {
                "model": self._config.embedding_model,
            },
        }
        if self._resolved_base_url:
            config["config"]["openai_base_url"] = self._resolved_base_url
        if self._resolved_api_key:
            config["config"]["api_key"] = self._resolved_api_key
        return config
