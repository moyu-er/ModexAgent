"""AgentMessageBus abstraction for decoupled inter-agent messaging."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from framework.messaging.broker import Address, BrokerMessage
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.session_id import DefaultSessionIdStrategy

if TYPE_CHECKING:
    from framework.messaging.broker import MessageBroker
    from framework.multi_agent.envelope import AgentMessageEnvelope
    from framework.multi_agent.inbox.consumer import BaseInboxConsumer
    from framework.multi_agent.inbox.producer import BaseInboxProducer

logger = logging.getLogger(__name__)


class AgentMessageBus(ABC):
    """Pluggable messaging facade for upper-layer multi-agent components.

    Decouples consumers from InboxServer and MessageBroker internals.
    """

    @abstractmethod
    async def send(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Send an envelope to the given session and signal the consumer."""
        ...

    @abstractmethod
    async def send_silent(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Persist the envelope without signaling/waking up the consumer."""
        ...

    @abstractmethod
    async def consume(
        self, session_id: str, limit: int = 100, *, block: bool = True
    ) -> list[AgentMessageEnvelope]:
        """Consume envelopes for the given session.

        When ``block=True``, the caller may block until messages are available.
        """
        ...

    @abstractmethod
    async def poll(self, session_id: str, limit: int = 100) -> list[AgentMessageEnvelope]:
        """Poll pending envelopes without blocking."""
        ...

    async def has_pending(self, session_id: str) -> bool:
        """Non-destructive check for pending messages (default: poll-based)."""
        return False

    @abstractmethod
    async def close(self) -> None:
        """Gracefully shut down the bus and unblock any waiting consumers."""
        ...


class LocalAgentMessageBus(AgentMessageBus):
    """Local implementation of AgentMessageBus.

    Responsibilities:
    1. Persist messages via InboxProducer.
    2. Signal cross-process consumers via MessageBroker wakeup messages.
    3. Signal same-process consumers via per-session asyncio.Event for low latency.

    .. note::
       The asyncio.Event optimization only works for consumers running in the
       *same process*. Cross-process consumers rely on the MessageBroker wakeup
       signal. This is a deliberate trade-off for local-first deployments.
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
        self._events: dict[str, asyncio.Event] = {}
        self._pending_counts: dict[str, int] = {}
        self._closed = False

    def _get_event(self, session_id: str) -> asyncio.Event:
        """Get or create the asyncio.Event for a session."""
        if session_id not in self._events:
            self._events[session_id] = asyncio.Event()
        return self._events[session_id]

    async def send(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Persist the envelope, then signal consumers through all available channels."""
        await self._producer.send(session_id, envelope)

        if self._broker is not None:
            try:
                wakeup = BrokerMessage(
                    payload={"_inbox_wakeup": True, "session_id": session_id},
                    sender=Address(kind="system", name="local_agent_message_bus"),
                )
                from framework.multi_agent.session_id import DefaultSessionIdStrategy
                try:
                    parts = DefaultSessionIdStrategy().parse(session_id)
                    target_name = parts.agent_name or (
                        envelope.target.name if envelope.target else session_id
                    )
                except ValueError:
                    target_name = (
                        envelope.target.name if envelope.target else str(session_id)
                    )
                await self._broker.send_to(
                    AgentAddress(kind="agent", name=target_name),
                    wakeup,
                )
            except Exception:
                logger.exception(
                    "Failed to send broker wakeup signal for session %s", session_id
                )

        event = self._get_event(session_id)
        self._pending_counts[session_id] = self._pending_counts.get(session_id, 0) + 1
        event.set()

    async def send_silent(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Persist the envelope without signaling/waking up the consumer.

        Updates pending_counts so that consume() correctly detects
        pending messages even when they were queued silently.
        """
        await self._producer.send(session_id, envelope)
        self._pending_counts[session_id] = self._pending_counts.get(session_id, 0) + 1

    async def consume(
        self, session_id: str, limit: int = 100, *, block: bool = True
    ) -> list[AgentMessageEnvelope]:
        """Return messages for ``session_id``, optionally blocking until available."""
        if block and not self._closed:
            event = self._get_event(session_id)
            if not event.is_set():
                try:
                    await event.wait()
                except asyncio.CancelledError:
                    raise
            # event will be cleared after consuming based on pending counts

        messages = await self._consumer.consume(session_id, limit)
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope

        envelopes: list[AgentMessageEnvelope] = []
        for msg in messages:
            payload = msg.metadata.get("payload") if msg.metadata else None
            if payload is None:
                payload = {"content": msg.content, "message_type": msg.message_type}
            envelope = AgentMessageEnvelope(
                payload=payload,
                source=AgentAddress(kind="agent", name=msg.source),
                message_type=msg.message_type,
                conversation_id=msg.metadata.get("conversation_id", session_id),
                agent_session_id=msg.metadata.get("agent_session_id", session_id),
                invocation_id=msg.metadata.get("invocation_id") if msg.metadata else None,
                message_id=msg.message_id,
                timestamp=msg.timestamp,
                metadata={k: v for k, v in msg.metadata.items() if k not in ("payload", "invocation_id")},
            )
            envelopes.append(envelope)

        # Adjust pending count and only clear event if no more pending messages
        event = self._get_event(session_id)
        remaining = self._pending_counts.get(session_id, 0) - len(messages)
        if remaining <= 0:
            self._pending_counts.pop(session_id, None)
            event.clear()
        else:
            self._pending_counts[session_id] = remaining

        return envelopes

    async def poll(self, session_id: str, limit: int = 100) -> list[AgentMessageEnvelope]:
        """Poll pending messages without blocking."""
        return await self.consume(session_id, limit=limit, block=False)

    async def has_pending(self, session_id: str) -> bool:
        """Non-destructive check using server count (does NOT consume messages)."""
        return await self._consumer.count(session_id) > 0

    async def close(self) -> None:
        """Set all registered events so blocked consumers can wake and exit."""
        self._closed = True
        for event in self._events.values():
            event.set()
