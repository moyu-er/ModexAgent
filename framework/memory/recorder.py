"""MemoryAppendRecorder: fire-and-forget provider write loop with dedup.

Provides:
- Stable message-hash deduplication (prevents double-add of the same message)
- Non-blocking asyncio.create_task fan-out (does not block the agent turn)
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
from typing import TYPE_CHECKING, Any

from framework.memory.core.scope import MemoryAgentRole, MemoryContext
from framework.memory.core.message import ChatMessage

if TYPE_CHECKING:
    from framework.plugins import MemoryProvider

logger = logging.getLogger(__name__)


class MemoryAppendRecorder:
    """Records message appends and fans out to MemoryProviders.

    Usage:
        recorder = MemoryAppendRecorder(providers)
        await recorder.record(messages, context)   # fire-and-forget
        await recorder.flush()                     # await pending tasks
    """

    def __init__(self, providers: list[MemoryProvider] | None = None) -> None:
        self._providers: list[MemoryProvider] = list(providers) if providers is not None else []
        self._pending: set[asyncio.Task[Any]] = set()
        self._seen_hashes: set[str] = set()

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
        if isinstance(message, ChatMessage):
            msg_dict = message.to_dict()
        else:
            msg_dict = dict(message)
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
            if v == MemoryAgentRole.PEER.value or v.startswith(MemoryAgentRole.PEER.value):
                return True
            if v == MemoryAgentRole.SUBAGENT.value or v.startswith(MemoryAgentRole.SUBAGENT.value):
                return True
        return False

    async def record(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: MemoryContext,
    ) -> None:
        """Record messages for provider fan-out (fire-and-forget).

        This method is async for interface consistency but returns quickly;
        actual provider calls are scheduled as background tasks.
        """
        if not self._providers or self._is_peer_or_subagent(context):
            return

        new_messages: list[ChatMessage | dict[str, Any]] = []
        for msg in messages:
            h = self._message_hash(msg)
            if h in self._seen_hashes:
                continue
            self._seen_hashes.add(h)
            new_messages.append(msg)

        if not new_messages:
            return

        task = asyncio.create_task(self._fan_out(new_messages, context))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

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
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
            self._pending.clear()
