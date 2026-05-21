"""MemoryAppendRecorder: fire-and-forget provider write loop with dedup.

Provides:
- Stable message-hash deduplication (prevents double-add of the same message)
- Scope-isolated dedup (same content in different scopes does not collide)
- Non-blocking asyncio.Queue + worker task (does not block the agent turn)
- peer/subagent auto-skip (only main agent writes to providers)
- replace_all bypass (bulk replacement does not trigger provider adds)
- Flush on close (awaits pending provider tasks before shutdown)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeAlias

from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryAgentRole, MemoryContext

if TYPE_CHECKING:
    from framework.plugins import MemoryProvider

QueuedAppend: TypeAlias = tuple[list[ChatMessage | dict[str, Any]], MemoryContext] | None

logger = logging.getLogger(__name__)


class MemoryAppendSource(StrEnum):
    """来源标记，用于区分消息进入短期记忆的路径。"""

    UNKNOWN = "unknown"
    USER_INPUT = "user_input"
    AGENT_APPEND = "agent_append"
    HISTORY_REPLACE = "history_replace"
    COMPACTION = "compaction"


class MemoryAppendRecorder:
    """Records message appends and fans out to MemoryProviders.

    Usage:
        recorder = MemoryAppendRecorder(providers)
        await recorder.record(messages, context)   # fire-and-forget
        await recorder.flush()                     # await pending tasks
    """

    def __init__(self, providers: list[MemoryProvider] | None = None) -> None:
        self._providers: list[MemoryProvider] = list(providers) if providers is not None else []
        self._seen_hashes: dict[str, set[str]] = {}  # scope_key -> hashes
        self._queue: asyncio.Queue[QueuedAppend] | None = None
        self._worker: asyncio.Task[Any] | None = None

    def add_provider(self, provider: MemoryProvider) -> None:
        """Register a provider for fan-out."""
        self._providers.append(provider)

    @property
    def providers(self) -> list[MemoryProvider]:
        """Return a copy of registered providers."""
        return list(self._providers)

    @staticmethod
    def _message_hash(message: ChatMessage | dict[str, Any]) -> str:
        """Stable hash of role + content (metadata excluded to avoid churn)."""
        # Normalize: ensure we work with dict
        msg_dict = message.to_dict() if isinstance(message, ChatMessage) else dict(message)
        # Exclude fields that may change between saves (metadata, timestamps)
        canonical = {
            "role": msg_dict.get("role", ""),
            "content": msg_dict.get("content", ""),
            "name": msg_dict.get("name"),          # tool name for tool results
            "tool_calls": msg_dict.get("tool_calls"),
            "tool_call_id": msg_dict.get("tool_call_id"),
        }
        # Drop None values for compact canonical form
        canonical = {k: v for k, v in canonical.items() if v is not None}
        try:
            payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = str(canonical)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_peer_or_subagent(context: MemoryContext) -> bool:
        """Return True if context belongs to a peer or subagent."""
        candidates = [
            context.agent_id,
            context.sender_agent,
            context.receiver_agent,
        ]
        for value in candidates:
            if not value:
                continue
            v = str(value).lower()
            if v == MemoryAgentRole.SUBAGENT.value or v.startswith(MemoryAgentRole.SUBAGENT.value):
                return True
            if v == "peer" or v.startswith("peer"):
                return True
        return False

    @staticmethod
    def _scope_key(context: MemoryContext) -> str:
        """Derive a stable scope key for dedup isolation."""
        # session_id is the finest granularity for short-term memory
        return context.session_id or "default"

    def _ensure_worker(self) -> None:
        """Lazily start the background worker task."""
        if self._worker is None or self._worker.done():
            self._queue = asyncio.Queue()
            self._worker = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        """Background worker: pulls batches from queue and fans out to providers."""
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item is None:  # sentinel
                break
            messages, context = item
            try:
                await self._fan_out(messages, context)
            except Exception:
                logger.exception("Recorder worker error")

    async def record(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: MemoryContext,
        source: MemoryAppendSource = MemoryAppendSource.UNKNOWN,
    ) -> None:
        """Record messages for provider fan-out (fire-and-forget).

        Messages are deduplicated per-scope and queued for a background worker
        to avoid blocking the agent turn.  Compaction-sourced messages are
        silently skipped because they represent already-processed history.
        """
        if not self._providers or self._is_peer_or_subagent(context):
            return

        # Skip compaction-sourced messages: they are internal rewrites, not
        # new conversational content that providers need to index.
        if source == MemoryAppendSource.COMPACTION:
            return

        scope_key = self._scope_key(context)
        seen = self._seen_hashes.setdefault(scope_key, set())

        new_messages: list[ChatMessage | dict[str, Any]] = []
        for msg in messages:
            h = self._message_hash(msg)
            if h in seen:
                continue
            seen.add(h)
            new_messages.append(msg)

        if not new_messages:
            return

        self._ensure_worker()
        assert self._queue is not None
        await self._queue.put((new_messages, context))

    async def _fan_out(
        self,
        messages: list[ChatMessage | dict[str, Any]],
        context: MemoryContext,
    ) -> None:
        dict_messages = [
            m.to_dict() if isinstance(m, ChatMessage) else dict(m) for m in messages
        ]
        for provider in self._providers:
            try:
                await provider.add(dict_messages, context)
            except Exception as e:
                logger.warning("Provider '%s' add failed: %s", provider.name, e)

    async def flush(self) -> None:
        """Await all pending provider tasks. Called during shutdown."""
        if self._worker and not self._worker.done():
            assert self._queue is not None
            await self._queue.put(None)  # sentinel to stop worker
            await self._worker
            self._worker = None
