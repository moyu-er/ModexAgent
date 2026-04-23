"""Embedding provider abstraction.

Two modes, switchable via config:
- local  (huggingface): sentence-transformers, auto-downloads model, fully offline
- remote (openai/…):    OpenAI-compatible API with configurable url + apiKey
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from ..config import Mem0Config

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    """Abstract embedding provider — lifecycle + mem0 config generation."""

    @abstractmethod
    def check_available(self) -> list[str]:
        """Return missing dependency names (empty = all available)."""

    @abstractmethod
    async def initialize(self, **kwargs: Any) -> None:
        """Download/load model or resolve remote config."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources (close, not delete)."""

    @abstractmethod
    def get_mem0_config(self) -> dict[str, Any]:
        """Return mem0 embedder config dict."""


def create_embedding_provider(config: Mem0Config) -> EmbeddingProvider:
    """Factory: pick the right embedding provider based on config."""
    if config.embedding_provider == "huggingface":
        from .local import LocalEmbeddingProvider

        return LocalEmbeddingProvider(config)

    from .remote import RemoteEmbeddingProvider

    return RemoteEmbeddingProvider(config)
