"""AgentMessageBus abstraction for decoupled inter-agent messaging."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from modex_agent.messaging.broker import AddressKind

if TYPE_CHECKING:
    from modex_agent.multi_agent.envelope import AgentMessageEnvelope
    from modex_agent.multi_agent.inbox.consumer import BaseInboxConsumer
    from modex_agent.multi_agent.inbox.producer import BaseInboxProducer
    from modex_agent.multi_agent.inbox.types import InboxMessage
    from modex_agent.multi_agent.inbox_poller import InboxPoller

logger = logging.getLogger(__name__)


class AgentMessageBus(ABC):
    """Pluggable messaging facade for upper-layer multi-agent components.

    The bus is event-driven with a tick fallback: producers persist envelopes
    via the inbox producer and signal the pool's ``InboxPoller`` so between-
    turn delivery starts with ~zero latency. The poller still ticks as a
    defensive fallback (covering any writer that bypasses ``send``).
    """

    @abstractmethod
    async def send(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Persist ``envelope`` for ``session_id`` and wake the pool poller."""
        ...

    @abstractmethod
    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[AgentMessageEnvelope]:
        """Consume and return up to ``limit`` envelopes for ``session_id``.

        Non-blocking: returns whatever is currently pending (possibly empty).
        ``only_types`` optionally filters by envelope ``message_type``.
        """
        ...

    async def peek(self, session_id: str, limit: int = 1) -> list[AgentMessageEnvelope]:
        """Non-destructive read of up to ``limit`` pending envelopes.

        Default returns empty; override for a real implementation. Used by the
        InboxPoller to read the parent link off the first pending envelope
        WITHOUT consuming the batch (so a materialize failure still leaves the
        messages in the inbox).
        """
        return []

    async def sessions_with_pending(self) -> list[str]:
        """Session ids with >=1 pending message (default empty; override for real)."""
        return []

    @abstractmethod
    async def close(self) -> None:
        """Gracefully shut down the bus."""
        ...


class LocalAgentMessageBus(AgentMessageBus):
    """Local event-driven implementation of AgentMessageBus.

    Responsibilities:
    1. Persist messages via the InboxProducer.
    2. Signal the pool's ``InboxPoller`` after a successful persist so it
       rescans immediately (single convergence point for every inbox writer:
       user input, agent-to-agent, CLI ``modexctl send``, external peer
       reply). The poller still ticks every ``interval`` as a defensive
       fallback for writers that bypass this bus.

    The poller is attached after construction via :meth:`set_poller` (it is
    created by the pool wiring, which runs after the bus). Until then ``send``
    is persist-only and the poller relies on its tick fallback.
    """

    def __init__(
        self,
        producer: BaseInboxProducer,
        consumer: BaseInboxConsumer,
    ) -> None:
        self._producer = producer
        self._consumer = consumer
        self._closed = False
        self._poller: InboxPoller | None = None

    def set_poller(self, poller: InboxPoller) -> None:
        """Wire the pool's ``InboxPoller`` so ``send`` can wake it directly.

        Called once by the pool wiring after both the bus and the poller exist.
        Idempotent: re-wiring just replaces the reference.
        """
        self._poller = poller

    async def send(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Persist the envelope, then wake the pool poller for immediate rescan.

        ``signal_wakeup`` is a non-blocking ``Event.set``; it never awaits the
        poller. If no poller is wired yet the call degrades to persist-only
        and the poller's tick fallback picks the message up within one
        ``interval``.
        """
        await self._producer.send(session_id, envelope)
        if self._poller is not None:
            self._poller.signal_wakeup()

    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[AgentMessageEnvelope]:
        """Return up to ``limit`` pending envelopes for ``session_id`` (non-blocking)."""
        messages = await self._consumer.consume(session_id, limit, only_types=only_types)
        return [self._reconstruct(msg, session_id) for msg in messages]

    async def peek(self, session_id: str, limit: int = 1) -> list[AgentMessageEnvelope]:
        """Non-destructive read of up to ``limit`` pending envelopes."""
        messages = await self._consumer.peek(session_id, limit=limit)
        return [self._reconstruct(msg, session_id) for msg in messages]

    @staticmethod
    def _reconstruct(msg: InboxMessage, session_id: str) -> AgentMessageEnvelope:
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.envelope import AgentMessageEnvelope

        payload = msg.metadata.get("payload") if msg.metadata else None
        if payload is None:
            payload = {"content": msg.content, "message_type": msg.message_type}
        # Preserve the original source kind/name (producer stores them in
        # metadata). Hardcoding kind="agent" here would erase the
        # channel/human origin of external_input envelopes, mis-classifying
        # human DMs as agent-source -> role=agent in session memory.
        meta = msg.metadata or {}
        src_kind_raw = meta.get("source_kind") or "agent"
        src_kind = AddressKind(src_kind_raw) if isinstance(src_kind_raw, str) else AddressKind.AGENT
        src_name = meta.get("source_name") or msg.source
        return AgentMessageEnvelope(
            payload=payload,
            source=AgentAddress(kind=src_kind, name=src_name),
            message_type=msg.message_type,
            session_id=msg.metadata.get("session_id", session_id),
            agent_session_id=msg.metadata.get("agent_session_id", session_id),
            parent_session_id=msg.metadata.get("parent_session_id") if msg.metadata else None,
            invocation_id=msg.metadata.get("invocation_id") if msg.metadata else None,
            message_id=msg.message_id,
            timestamp=msg.timestamp,
            metadata={
                k: v
                for k, v in msg.metadata.items()
                if k not in ("payload", "invocation_id", "parent_session_id")
            },
        )

    async def sessions_with_pending(self) -> list[str]:  # type: ignore[override]
        """Forward to the consumer's session enumeration."""
        return await self._consumer.sessions_with_pending()

    async def close(self) -> None:
        """Mark the bus as closed."""
        self._closed = True
