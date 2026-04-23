"""Mem0 semantic memory provider — RAG-style memory retrieval.

Integrates into the framework's MemorySystem via the plugin system.
The four-layer memory architecture is untouched; this provider is an
additive enhancement layer.

Key integration points (all via existing fan-out, no framework changes):
- add()              → MemorySystem.add_messages() fan-out
- search()           → MemorySystem.search_memories() fan-out
- prefetch()         → DefaultMemoryInjectionPolicy → <memory-context> injection
- on_pre_compress()  → ShortTermMemoryManager compression callback
- system_prompt_block() → MemorySystem.build_system_prompt() section
"""

import asyncio
import contextlib
import importlib.util
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from framework.plugins.abc import MemoryProvider

from .config import Mem0Config
from .embedding import create_embedding_provider
from .utils import convert_messages, format_prefetch

if TYPE_CHECKING:
    from framework.memory.core.scope import MemoryContext

logger = logging.getLogger(__name__)


class Mem0MemoryProvider(MemoryProvider):
    """Mem0-backed semantic memory provider.

    Storage: pure local (ChromaDB files + SQLite), no external services required.
    """

    def __init__(self, config: Mem0Config):
        self._config = config
        self._mem0: Any = None
        self._embedding = create_embedding_provider(config)

    @property
    def name(self) -> str:
        return "mem0"

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def is_available(self) -> bool:
        missing = []
        for pkg in ("mem0", "chromadb"):
            if importlib.util.find_spec(pkg) is None:
                missing.append(pkg)
        missing.extend(self._embedding.check_available())
        if missing:
            install_names = [p + "ai" if p == "mem0" else p for p in missing]
            logger.warning(
                "%s not installed — mem0 provider disabled. "
                "Install with: pip install %s",
                " & ".join(missing),
                " ".join(install_names),
            )
            return False
        return True

    async def initialize(self, **kwargs: Any) -> None:
        from mem0 import Memory

        if self._config.disable_telemetry:
            os.environ["MEM0_TELEMETRY"] = "False"

        # Initialize embedding provider (download model / resolve config)
        await self._embedding.initialize(**kwargs)

        workspace = Path(self._config.workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        mem0_config = {
            "vector_store": {
                "provider": self._config.vector_store,
                "config": {
                    "collection_name": self._config.collection_name,
                    "path": str(workspace / "vectors"),
                },
            },
            "embedder": self._embedding.get_mem0_config(),
            "llm": self._build_llm_config(kwargs),
            "history_db_path": str(workspace / "mem0_history.db"),
            "version": "v1.1",
        }

        self._mem0 = await asyncio.to_thread(Memory.from_config, mem0_config)
        logger.info("Mem0 initialized — workspace=%s", workspace)

    async def shutdown(self) -> None:
        if self._mem0 is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._mem0.close)
            self._mem0 = None
        await self._embedding.shutdown()
        logger.info("Mem0 shut down")

    # ------------------------------------------------------------------ #
    #  Core capabilities                                                  #
    # ------------------------------------------------------------------ #

    async def add(
        self,
        messages: list[dict[str, Any]],
        context: "MemoryContext",
    ) -> dict[str, Any]:
        """ENTRY POINT #1 — Per-turn message save into mem0.

        Called by: MemorySystem.add_messages() → fan-out to all providers.
        Trigger:   After every user/assistant message is saved to short-term memory.
        Flow:      convert_messages() filters → mem0.add() extracts facts via LLM
                   → vectorized + stored in ChromaDB + deduplicated.

        What gets saved:
          - user messages: facts about the user (preferences, identity, plans)
          - assistant messages: facts the assistant stated (recommendations,
            confirmed information) — mem0 official prompt says "extract from BOTH"
          - tool messages: FILTERED (see convert_messages() for rationale)
        """
        if self._mem0 is None:
            return {"status": "error", "error": "not initialized"}

        mem0_msgs = convert_messages(messages)
        if not mem0_msgs:
            return {"status": "ok", "memories": []}

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._mem0.add,
                    mem0_msgs,
                    user_id=context.user_id or "default",
                    agent_id=context.agent_id,
                    metadata={
                        k: v
                        for k, v in {
                            "session_id": context.session_id,
                            "channel": context.channel,
                        }.items()
                        if v is not None
                    },
                ),
                timeout=self._config.operation_timeout,
            )
            facts = result.get("results", [])
            logger.info(
                "[mem0] add: %d messages → %d facts extracted (user=%s, session=%s)",
                len(mem0_msgs), len(facts), context.user_id or "default", context.session_id,
            )
            for f in facts:
                logger.debug("[mem0]   fact: %s", f.get("memory", ""))
            return {"status": "ok", "memories": facts}
        except asyncio.TimeoutError:
            logger.warning("Mem0 add timed out (%.0fs)", self._config.operation_timeout)
            return {"status": "error", "error": "timeout"}
        except Exception as e:
            logger.warning("Mem0 add failed: %s", e)
            return {"status": "error", "error": str(e)}

    async def search(
        self,
        query: str,
        context: "MemoryContext",
        limit: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic + hybrid search via mem0 (semantic + BM25 + entity boost)."""
        if self._mem0 is None:
            return []

        try:
            # mem0 v2.x: user_id/agent_id must be in filters, not top-level params
            merged_filters: dict[str, Any] = {
                "user_id": context.user_id or "default",
            }
            if context.agent_id:
                merged_filters["agent_id"] = context.agent_id
            if filters:
                merged_filters.update(filters)
            results = await asyncio.wait_for(
                asyncio.to_thread(
                    self._mem0.search,
                    query=query,
                    limit=limit,
                    filters=merged_filters,
                ),
                timeout=self._config.operation_timeout,
            )
            items = [
                {
                    "memory": r.get("memory", ""),
                    "score": r.get("score", 0),
                    "metadata": r.get("metadata", {}),
                }
                for r in results.get("results", [])
            ]
            logger.info(
                "[mem0] search: query='%s' → %d results (user=%s)",
                query[:50], len(items), context.user_id or "default",
            )
            for item in items:
                logger.debug("[mem0]   %.2f: %s", item["score"], item["memory"][:80])
            return items
        except asyncio.TimeoutError:
            logger.warning("Mem0 search timed out (%.0fs)", self._config.operation_timeout)
            return []
        except Exception as e:
            logger.warning("Mem0 search failed: %s", e)
            return []

    # ------------------------------------------------------------------ #
    #  Per-turn injection (the key integration point)                     #
    # ------------------------------------------------------------------ #

    async def prefetch(
        self,
        query: str,
        context: "MemoryContext",
    ) -> str | None:
        """Per-turn semantic memory retrieval → injected into <memory-context>.

        Retrieves two kinds of memories:
        1. Core memories — user's top recent facts (always present)
        2. Search results — facts relevant to the current query (dynamic)

        Deduplicates by memory text, filters by score threshold.
        """
        if self._mem0 is None or not query:
            return None

        try:
            # mem0 v2.x: user_id/agent_id must be in filters for get_all too
            core_filters = {"user_id": context.user_id or "default"}
            if context.agent_id:
                core_filters["agent_id"] = context.agent_id
            core_task = asyncio.to_thread(
                self._mem0.get_all,
                filters=core_filters,
                limit=3,
            )
            search_task = asyncio.to_thread(
                self._mem0.search,
                query=query,
                user_id=context.user_id or "default",
                agent_id=context.agent_id,
                limit=self._config.prefetch_top_k,
            )
            core_result, search_result = await asyncio.wait_for(
                asyncio.gather(core_task, search_task, return_exceptions=True),
                timeout=self._config.operation_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Mem0 prefetch timed out (%.0fs)", self._config.operation_timeout)
            return None
        except Exception as e:
            logger.warning("Mem0 prefetch failed: %s", e)
            return None

        seen: set[str] = set()
        merged: list[dict] = []

        if isinstance(core_result, dict):
            for m in core_result.get("results", []):
                text = m.get("memory", "")
                if text and text not in seen:
                    seen.add(text)
                    merged.append(m)

        if isinstance(search_result, dict):
            for m in search_result.get("results", []):
                text = m.get("memory", "")
                score = m.get("score", 0)
                if text and text not in seen and score >= self._config.prefetch_min_score:
                    seen.add(text)
                    merged.append(m)

        if not merged:
            logger.debug("[mem0] prefetch: query='%s' → no relevant memories", query[:50])
            return None

        logger.info(
            "[mem0] prefetch: query='%s' → %d memories injected into <memory-context> (user=%s)",
            query[:50], len(merged), context.user_id or "default",
        )
        for m in merged:
            score = m.get("score", 0)
            logger.debug("[mem0]   %.2f: %s", score, m.get("memory", "")[:80])

        return format_prefetch(merged)

    # ------------------------------------------------------------------ #
    #  Pre-compress extraction                                            #
    # ------------------------------------------------------------------ #

    async def on_pre_compress(
        self,
        messages: list[dict[str, Any]],
        context: "MemoryContext",
    ) -> None:
        """ENTRY POINT #2 — Rescue facts before short-term compression prunes them.

        Called by: ShortTermMemoryManager._maybe_compress() → pre_compress callbacks.
        Trigger:   When short-term memory exceeds max_messages or max_tokens.
        Flow:      convert_messages() filters → mem0.add() extracts facts → stored.

        Why this matters:
          Short-term memory is limited (default 50 messages / token budget).
          Old messages get compressed/archived and eventually lost. This callback
          fires BEFORE pruning, giving mem0 a chance to extract and persist
          important facts into the vector store for long-term retrieval.

        What gets saved: same filtering as add() — user + assistant only.
        """
        if self._mem0 is None:
            return

        mem0_msgs = convert_messages(messages)
        if not mem0_msgs:
            return

        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self._mem0.add,
                    mem0_msgs,
                    user_id=context.user_id or "default",
                    agent_id=context.agent_id,
                    metadata={
                        k: v
                        for k, v in {
                            "source": "pre_compress",
                            "session_id": context.session_id,
                        }.items()
                        if v is not None
                    },
                ),
                timeout=self._config.operation_timeout,
            )
            logger.info(
                "[mem0] on_pre_compress: extracted facts from %d messages before compression (session=%s)",
                len(mem0_msgs), context.session_id,
            )
        except asyncio.TimeoutError:
            logger.warning("Mem0 on_pre_compress timed out (%.0fs)", self._config.operation_timeout)
        except Exception as e:
            logger.warning("Mem0 on_pre_compress failed: %s", e)

    # ------------------------------------------------------------------ #
    #  System prompt                                                      #
    # ------------------------------------------------------------------ #

    def system_prompt_block(self) -> str:
        """Static text injected into system prompt."""
        return (
            "## 语义记忆\n"
            "系统会自动记住你提到的重要信息和偏好。"
            "当上下文相关时，系统会注入相关的历史记忆辅助回复。\n"
            "你可以随时纠正或补充你的偏好，系统会自动更新记忆。"
        )

    # ------------------------------------------------------------------ #
    #  Internal — config builders                                         #
    # ------------------------------------------------------------------ #

    def _build_llm_config(self, kwargs: dict) -> dict:
        """Build mem0 LLM config for fact extraction.

        Strips litellm provider prefix: "openai/MiniMax-M2.5" → "MiniMax-M2.5".
        """
        llm_provider = kwargs.get("llm_provider")
        if llm_provider is None:
            logger.warning("No llm_provider passed — mem0 will use default LLM config")
            return {}

        model = getattr(llm_provider, "model", "gpt-4o-mini")
        api_key = getattr(llm_provider, "api_key", None)
        base_url = getattr(llm_provider, "base_url", None)

        if "/" in model:
            model = model.split("/", 1)[1]

        config: dict[str, Any] = {
            "provider": self._config.llm_provider_name,
            "config": {"model": model},
        }
        if api_key:
            config["config"]["api_key"] = api_key
        if base_url:
            config["config"]["openai_base_url"] = base_url

        return config
