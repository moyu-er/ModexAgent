"""Internal agent communication service — routing logic shared by sync and async tools.

This service owns target validation, UUID semantics, session ID construction,
envelope building, and delivery. Tool classes become thin wrappers around it.
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.session_id import DefaultSessionIdStrategy

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.messaging.broker import MessageBroker
    from framework.multi_agent.address import AgentAddress
    from framework.multi_agent.bus import AgentMessageBus
    from framework.multi_agent.comm_tracker import CommunicationTracker
    from framework.multi_agent.registry import AgentRegistry

logger = logging.getLogger(__name__)

_TASK_UUID_BYTES = 8


@dataclass(frozen=True)
class AgentSendResult:
    """Result returned by AgentCommunicationService after a successful send."""

    target_agent: str
    target_kind: AgentCommKind
    session_id: str
    uuid: str | None
    created_new_task: bool


class AgentCommunicationService:
    """Internal service for inter-agent communication routing.

    Owns validation, UUID semantics, session ID building, envelope construction,
    and sync/async delivery selection. Tool classes delegate to this service.
    """

    def __init__(
        self,
        source: AgentAddress,
        broker: MessageBroker,
        registry: AgentRegistry,
        *,
        agent_bus: AgentMessageBus | None = None,
        session_strategy: DefaultSessionIdStrategy | None = None,
        comm_tracker: CommunicationTracker | None = None,
    ) -> None:
        self._source = source
        self._broker = broker
        self._registry = registry
        self._agent_bus = agent_bus
        self._session_strategy = session_strategy or DefaultSessionIdStrategy()
        self._comm_tracker = comm_tracker

    def _resolve_target_kind(self, target_agent: str) -> AgentCommKind | None:
        """Look up target's AgentCommKind from the registry."""
        descriptor = self._registry.get_descriptor(target_agent)
        if descriptor is not None:
            return descriptor.comm_kind
        profile = self._registry.get_profile(target_agent)
        if profile is not None:
            return profile.comm_kind
        return None

    def _validate_uuid(
        self,
        uuid_in: str | None,
        target_kind: AgentCommKind,
    ) -> tuple[str | None, str | None]:
        """Validate uuid against target kind. Returns (normalized_uuid, error).

        Rules:
        - NORMAL target: uuid must be None.
        - SUBAGENT target: uuid must not be None. "" generates a new uuid.
        """
        if target_kind == AgentCommKind.NORMAL:
            if uuid_in is not None:
                return None, f"Cannot send with uuid to a normal agent ({target_kind.value})"
            return None, None

        if target_kind == AgentCommKind.SUBAGENT:
            if uuid_in is None:
                return None, "uuid is required for subagent targets"
            if uuid_in == "":
                new_uuid = _uuid_mod.uuid4().hex[:_TASK_UUID_BYTES]
                return new_uuid, None
            return uuid_in, None

        return None, f"Unknown target kind: {target_kind!r}"

    async def send_sync(
        self,
        *,
        target_agent: str,
        content: str,
        uuid: str | None,
        context: AgentContext,
    ) -> str:
        """Send synchronously via broker wakeup. Returns result text."""
        result = await self._send(
            target_agent=target_agent,
            content=content,
            uuid=uuid,
            context=context,
            async_mode=False,
        )
        if result is None:
            return f"Error: target agent '{target_agent}' not found"
        return f"Message sent to {result.target_agent}." + (
            f" uuid: {result.uuid}" if result.uuid else ""
        )

    async def send_async(
        self,
        *,
        target_agent: str,
        content: str,
        uuid: str | None,
        context: AgentContext,
    ) -> str:
        """Send asynchronously via inbox. Returns acknowledgement text."""
        result = await self._send(
            target_agent=target_agent,
            content=content,
            uuid=uuid,
            context=context,
            async_mode=True,
        )
        if result is None:
            return f"Error: target agent '{target_agent}' not found"
        return f"Message sent to {result.target_agent}." + (
            f" uuid: {result.uuid}" if result.uuid else ""
        )

    async def _send(
        self,
        *,
        target_agent: str,
        content: str,
        uuid: str | None,
        context: AgentContext,
        async_mode: bool,
    ) -> AgentSendResult | None:
        """Core routing logic shared by sync and async sends."""
        # 1. Validate context
        session_meta = context.session_meta
        if session_meta is None:
            return None

        conversation_id = session_meta.conversation_id

        # 2. Look up target
        target_kind = self._resolve_target_kind(target_agent)
        if target_kind is None:
            # Check if target exists at all
            available = [p.name for p in self._registry.list_profiles()]
            if target_agent not in available:
                return None  # caller should report error
            target_kind = AgentCommKind.NORMAL  # fallback default

        # 3. Validate uuid
        normalized_uuid, error = self._validate_uuid(uuid, target_kind)
        if error is not None:
            return None  # caller should report error

        created_new_task = uuid == "" and target_kind == AgentCommKind.SUBAGENT

        # 4. Build session ID (receiver-owned)
        session_id = self._session_strategy.format(
            conversation_id=conversation_id,
            agent_name=target_agent,
            uuid=normalized_uuid,
        )

        # 5. Build envelope
        # For subagent replying to normal parent: preserve caller's uuid on envelope
        envelope_uuid = normalized_uuid
        if target_kind == AgentCommKind.NORMAL and session_meta.comm_kind == AgentCommKind.SUBAGENT:
            envelope_uuid = session_meta.uuid

        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope

        envelope = AgentMessageEnvelope(
            payload={"content": content, "message_type": "agent_message"},
            source=self._source,
            target=AgentAddress(kind="agent", name=target_agent),
            message_type="agent_message",
            conversation_id=conversation_id,
            agent_session_id=session_id,
            uuid=envelope_uuid,
        )

        # 6. Record communication tracker events
        if self._comm_tracker is not None and envelope.uuid is not None:
            self._comm_tracker.record_send(
                agent_name=self._source.name,
                target_agent=target_agent,
                invocation_id=envelope.uuid,
                session_id=session_id,
                content_summary=content[:500],
            )

        # 7. Deliver
        if async_mode and self._agent_bus is not None:
            inbox_key = self._session_strategy.format(
                conversation_id=conversation_id, agent_name=target_agent,
            )
            await self._agent_bus.send_silent(inbox_key, envelope)
        else:
            await self._broker.send_to(envelope.target, envelope.to_broker_message())

        return AgentSendResult(
            target_agent=target_agent,
            target_kind=target_kind,
            session_id=session_id,
            uuid=normalized_uuid,
            created_new_task=created_new_task,
        )

    def build_targets_description(self) -> str:
        """Build a description of available targets with their kind for the LLM."""
        profiles = self._registry.list_profiles()
        if not profiles:
            return "No agents available."

        lines = ["Available targets:"]
        for p in profiles:
            lines.append(f"- {p.name} ({p.comm_kind.value})")
        lines.append("")
        lines.append("Use uuid=null when sending to a normal agent.")
        lines.append('Use uuid="" when starting a new task for a subagent.')
        lines.append("Use uuid=\"<existing uuid>\" when continuing a subagent task.")
        return "\n".join(lines)
