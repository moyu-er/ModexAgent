"""Local embedding provider — sentence-transformers with mirror and offline support."""

import asyncio
import gc
import importlib.util
import logging
import os
from pathlib import Path
from typing import Any

from ..config import Mem0Config
from . import EmbeddingProvider

logger = logging.getLogger(__name__)


class LocalEmbeddingProvider(EmbeddingProvider):
    """Local sentence-transformers embedding.

    Features:
    - Auto-downloads model on first use (with mirror support)
    - Supports local directory path as model name
    - Prefers offline cache if model already downloaded
    - Pre-downloads and verifies model during initialize()
    """

    def __init__(self, config: Mem0Config):
        self._config = config

    def check_available(self) -> list[str]:
        if importlib.util.find_spec("sentence_transformers") is None:
            return ["sentence-transformers"]
        return []

    async def initialize(self, **kwargs: Any) -> None:
        """Pre-download model and warm-up to verify it works."""
        from sentence_transformers import SentenceTransformer

        model_name = self._config.embedding_model
        is_local = Path(model_name).exists()

        # Set up environment before any HF operations
        self._configure_env()

        if is_local:
            logger.info("Loading embedding model from local path: %s", model_name)
            model = await asyncio.to_thread(SentenceTransformer, model_name)
        else:
            logger.info("Loading embedding model: %s (cache_dir=%s)", model_name, self._config.embedding_cache_dir)
            # Try offline first (uses cache if available, no network)
            model = await self._load_with_fallback(SentenceTransformer, model_name)

        # Warm-up: verify the model produces valid embeddings
        await asyncio.to_thread(model.encode, ["warm-up check"])
        logger.info("Embedding model verified: %s", model_name)

        # Release — mem0 will load its own instance from cache
        del model
        gc.collect()

        # Model is cached and verified — force ALL subsequent HF operations offline.
        # This prevents mem0's internal SentenceTransformer from hitting the network.
        os.environ["HF_HUB_OFFLINE"] = "1"
        logger.info("Model cached, set HF_HUB_OFFLINE=1 to prevent network access")

    def _configure_env(self) -> None:
        """Set environment variables for mirror/cache before any HF operations."""
        if self._config.embedding_mirror:
            os.environ["HF_ENDPOINT"] = self._config.embedding_mirror
            logger.info("HuggingFace mirror: %s", self._config.embedding_mirror)

        if self._config.embedding_cache_dir:
            cache_dir = str(Path(self._config.embedding_cache_dir).resolve())
            os.environ["SENTENCE_TRANSFORMERS_HOME"] = cache_dir
            # Ensure directory exists
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            logger.info("Embedding cache dir: %s", cache_dir)

    async def _load_with_fallback(self, cls: type, model_name: str) -> Any:
        """Try offline first, then online download."""
        # 1. Try offline (no network, uses cache only)
        try:
            model = await asyncio.to_thread(
                cls, model_name, local_files_only=True,
            )
            logger.info("Model loaded from cache (offline): %s", model_name)
            return model
        except Exception:
            logger.debug("Model not in cache, downloading: %s", model_name)

        # 2. Download from HuggingFace Hub (or mirror)
        model = await asyncio.to_thread(cls, model_name)
        return model

    async def shutdown(self) -> None:
        """No-op: mem0 manages the loaded model via its close() method."""

    def get_mem0_config(self) -> dict[str, Any]:
        return {
            "provider": "huggingface",
            "config": {
                "model": self._config.embedding_model,
            },
        }
