"""Mem0 semantic memory plugin — RAG-style memory retrieval for the bot project.

Provides:
1. Automatic fact extraction from conversations (via mem0's LLM pipeline)
2. Vector-based semantic search (ChromaDB local file storage)
3. Per-turn memory injection via prefetch() → <memory-context> tags
4. Pre-compress fact extraction to preserve memories before context compression

Storage: pure local (ChromaDB files + SQLite), no external services required.
"""

import logging

from framework.plugins.context import PluginContext

from .config import Mem0Config
from .provider import Mem0MemoryProvider

logger = logging.getLogger(__name__)


def register(ctx: PluginContext) -> None:
    """Plugin registration — called by PluginManager during discovery."""
    if not ctx.get_config("enabled", True):
        logger.info("mem0_memory plugin is disabled in config, skipping registration")
        return

    config = Mem0Config(
        workspace=ctx.get_config("workspace", "./data/vector_memory"),
        vector_store=ctx.get_config("vector_store", "chroma"),
        collection_name=ctx.get_config("collection_name", "bot_memories"),
        embedding_provider=ctx.get_config("embedding_provider", "huggingface"),
        embedding_model=ctx.get_config("embedding_model", "sentence-transformers/all-MiniLM-L6-v2"),
        embedding_base_url=ctx.get_config("embedding_base_url", None),
        embedding_api_key=ctx.get_config("embedding_api_key", None),
        embedding_cache_dir=ctx.get_config("embedding_cache_dir", None),
        embedding_mirror=ctx.get_config("embedding_mirror", None),
        llm_provider_name=ctx.get_config("llm_provider_name", "openai"),
        search_top_k=ctx.get_config("search_top_k", 5),
        prefetch_top_k=ctx.get_config("prefetch_top_k", 5),
        prefetch_min_score=ctx.get_config("prefetch_min_score", 0.3),
        operation_timeout=ctx.get_config("operation_timeout", 15.0),
        disable_telemetry=ctx.get_config("disable_telemetry", True),
    )
    ctx.register_memory_provider(Mem0MemoryProvider(config))
    logger.info("Registered mem0_memory provider (workspace=%s)", config.workspace)
