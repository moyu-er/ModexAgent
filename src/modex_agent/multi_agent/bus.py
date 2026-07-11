"""AgentMessageBus abstraction for decoupled inter-agent messaging."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from modex_agent.messaging.broker import Address, AddressKind, BrokerMessage
from modex_agent.multi_agent.address import AgentAddress

if TYPE_CHECKING:
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent.envelope import AgentMessageEnvelope
    from modex_agent.multi_agent.inbox.consumer import BaseInboxConsumer
    from modex_agent.multi_agent.inbox.producer import BaseInboxProducer

logger = logging.getLogger(__name__)


class AgentMessageBus(ABC):
    """Pluggable messaging facade for upper-layer multi-agent components.

    The bus is poll-driven: producers persist envelopes via the inbox
    producer, and an ``InboxPoller`` (per pool) drives between-turn
    consumption. Cross-process latency is handled by a broker
    ``_inbox_wakeup`` message emitted from ``send``.
    """

    @abstractmethod
    async def send(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Persist ``envelope`` for ``session_id`` and wake cross-process consumers."""
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
    """Local poll-driven implementation of AgentMessageBus.

    Responsibilities:
    1. Persist messages via the InboxProducer.
    2. Emit a broker ``_inbox_wakeup`` for cross-process consumers when a
       broker is configured (single-process deployments are poller-only).

    Between-turn delivery is driven by an ``InboxPoller`` polling
    ``consume``; there is no in-process signal/Event wakeup path.
    """

    def __init__(
        self,
        producer: BaseInboxProducer,
        consumer: BaseInboxConsumer,
        broker: MessageBroker | None = None,
    ) -> None:
        self._producer = producer
        self._consumer = consumer
        self._broker = broker
        self._closed = False

    async def send(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Persist the envelope, then emit a broker wakeup for cross-process consumers.

        NOTE: the broker ``_inbox_wakeup`` is emitted here but no handler
        consumes it yet — cross-process wakeup is deferred. Single-process
        deployments are poller-only (the ``InboxPoller`` ticks ~200ms and
        re-scans regardless), so messages are never lost; multi-process
        deployments simply wait up to one tick for delivery. Wiring a wakeup
        handler that pokes the target pool's poller is a tracked follow-up.
        """
        await self._producer.send(session_id, envelope)
        if self._broker is not None:
            try:
                wakeup = BrokerMessage(
                    payload={"_inbox_wakeup": True, "session_id": session_id},
                    sender=Address(kind=AddressKind.SYSTEM, name="local_agent_message_bus"),
                )
                target_name = envelope.target.name if envelope.target else session_id
                await self._broker.send_to(
                    AgentAddress(kind=AddressKind.AGENT, name=target_name),
                    wakeup,
                )
            except Exception:
                logger.exception(
                    "Failed to send broker wakeup signal for session %s",
                    session_id,
                )

    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[AgentMessageEnvelope]:
        """Return up to ``limit`` pending envelopes for ``session_id`` (non-blocking)."""
        messages = await self._consumer.consume(
            session_id, limit, only_types=only_types
        )
        return [self._reconstruct(msg, session_id) for msg in messages]

    async def peek(self, session_id: str, limit: int = 1) -> list[AgentMessageEnvelope]:
        """Non-destructive read of up to ``limit`` pending envelopes."""
        messages = await self._consumer.peek(session_id, limit=limit)
        return [self._reconstruct(msg, session_id) for msg in messages]

    @staticmethod
    def _reconstruct(msg, session_id: str) -> AgentMessageEnvelope:
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
            parent_session_id=msg.metadata.get("parent_session_id")
            if msg.metadata
            else None,
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
