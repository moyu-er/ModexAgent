"""Mem0 plugin configuration model."""

from dataclasses import dataclass


@dataclass
class Mem0Config:
    """Configuration read from bot_config.yml → plugins.configurations.mem0_memory."""

    # Storage
    workspace: str = "./data/vector_memory"
    vector_store: str = "chroma"  # chroma | faiss
    collection_name: str = "bot_memories"

    # Embedding model
    # "huggingface" → sentence-transformers local (offline, no API key)
    # "openai" → OpenAI-compatible API (needs api_key/base_url)
    embedding_provider: str = "huggingface"
    # Model name (HuggingFace repo ID) OR local directory path
    # Examples: "sentence-transformers/all-MiniLM-L6-v2" or "./data/embedding_models/all-MiniLM-L6-v2"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None

    # Local embedding cache directory (default: HuggingFace default cache)
    # If set, models are stored here and offline loading is preferred
    embedding_cache_dir: str | None = None

    # HuggingFace mirror site (for users behind GFW)
    # Common: "https://hf-mirror.com"
    embedding_mirror: str | None = None

    # Per-operation timeout (seconds) — prevents slow embedding from blocking pipeline
    operation_timeout: float = 15.0

    # LLM for fact extraction (mem0 internal use, reuses framework's provider)
    llm_provider_name: str = "openai"  # mem0's internal provider name

    # Retrieval tuning
    search_top_k: int = 5
    prefetch_top_k: int = 5
    prefetch_min_score: float = 0.3

    # Misc
    disable_telemetry: bool = True
