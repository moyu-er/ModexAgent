"""Internal agent communication service — routing logic shared by sync and async tools.

This service owns target validation, invocation_id semantics, session ID construction,
envelope building, and delivery. Tool classes become thin wrappers around it.
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from framework.multi_agent.address import AgentAddress
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.multi_agent.session_id import DefaultSessionIdStrategy

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.messaging.broker import MessageBroker
    from framework.multi_agent.address import AgentAddress
    from framework.multi_agent.bus import AgentMessageBus
    from framework.multi_agent.comm_tracker import CommunicationTracker
    from framework.multi_agent.registry import AgentRegistry

logger = logging.getLogger(__name__)

_TASK_ID_BYTES = 8


@dataclass(frozen=True)
class AgentSendResult:
    """Result returned by AgentCommunicationService after a send attempt."""

    target_agent: str
    target_kind: AgentCommKind
    session_id: str
    invocation_id: str | None
    created_new_task: bool
    error: str | None = None


class AgentCommunicationService:
    """Internal service for inter-agent communication routing.

    Owns validation, invocation_id semantics, session ID building, envelope construction,
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

    def _validate_invocation_id(
        self,
        invocation_id_in: str | None,
        target_kind: AgentCommKind,
    ) -> tuple[str | None, str | None]:
        """Validate invocation_id against target kind. Returns (normalized_invocation_id, error).

        Rules:
        - NORMAL target: invocation_id must be None.
        - SUBAGENT target: invocation_id must not be None. "" generates a new uuid.
        """
        if target_kind == AgentCommKind.NORMAL:
            if invocation_id_in is not None:
                return None, f"Cannot send with uuid to a normal agent ({target_kind.value})"
            return None, None

        if target_kind == AgentCommKind.SUBAGENT:
            if invocation_id_in is None:
                return None, "invocation_id is required for subagent targets"
            if invocation_id_in == "":
                new_invocation_id = _uuid_mod.uuid4().hex[:_TASK_ID_BYTES]
                return new_invocation_id, None
            return invocation_id_in, None

        return None, f"Unknown target kind: {target_kind!r}"

    async def send_sync(
        self,
        *,
        target_agent: str,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> str:
        """Send synchronously via broker wakeup. Returns result text."""
        result = await self._send(
            target_agent=target_agent,
            content=content,
            invocation_id=invocation_id,
            context=context,
            async_mode=False,
        )
        if result.error:
            return f"Error: {result.error}"
        return f"Message sent to {result.target_agent}." + (
            f" invocation_id: {result.invocation_id}" if result.invocation_id else ""
        )

    async def send_async(
        self,
        *,
        target_agent: str,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> str:
        """Send asynchronously via inbox. Returns acknowledgement text."""
        result = await self._send(
            target_agent=target_agent,
            content=content,
            invocation_id=invocation_id,
            context=context,
            async_mode=True,
        )
        if result.error:
            return f"Error: {result.error}"
        return f"Message sent to {result.target_agent}." + (
            f" invocation_id: {result.invocation_id}" if result.invocation_id else ""
        )

    async def _send(
        self,
        *,
        target_agent: str,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
        async_mode: bool,
    ) -> AgentSendResult | None:
        """Core routing logic shared by sync and async sends."""
        # 1. Validate context
        session_meta = context.session_meta
        if session_meta is None:
            return AgentSendResult(
                target_agent=target_agent, target_kind=AgentCommKind.NORMAL,
                session_id="", invocation_id=None, created_new_task=False,
                error="No agent session metadata available",
            )

        conversation_id = session_meta.conversation_id

        # 2. Look up target
        target_kind = self._resolve_target_kind(target_agent)
        if target_kind is None:
            available = [p.name for p in self._registry.list_profiles()]
            if target_agent not in available:
                return AgentSendResult(
                    target_agent=target_agent, target_kind=AgentCommKind.NORMAL,
                    session_id="", invocation_id=None, created_new_task=False,
                    error=f"Target agent '{target_agent}' not found",
                )
            target_kind = AgentCommKind.NORMAL

        # 3. Validate invocation_id
        normalized_invocation_id, error = self._validate_invocation_id(invocation_id, target_kind)
        if error is not None:
            return AgentSendResult(
                target_agent=target_agent, target_kind=target_kind,
                session_id="", invocation_id=None, created_new_task=False,
                error=error,
            )

        created_new_task = invocation_id == "" and target_kind == AgentCommKind.SUBAGENT

        # 4. Build session ID (receiver-owned)
        session_id = self._session_strategy.format(
            conversation_id=conversation_id,
            agent_name=target_agent,
            invocation_id=normalized_invocation_id,
        )

        # 5. Build envelope
        # For subagent replying to normal parent: preserve caller's uuid on envelope
        envelope_invocation_id = normalized_invocation_id
        if target_kind == AgentCommKind.NORMAL and session_meta.comm_kind == AgentCommKind.SUBAGENT:
            envelope_invocation_id = session_meta.invocation_id

        envelope = AgentMessageEnvelope(
            payload={"content": content, "message_type": "agent_message"},
            source=self._source,
            target=AgentAddress(kind="agent", name=target_agent),
            message_type="agent_message",
            conversation_id=conversation_id,
            agent_session_id=session_id,
            invocation_id=envelope_invocation_id,
        )

        # 6. Record communication tracker events
        if self._comm_tracker is not None and envelope.invocation_id is not None:
            self._comm_tracker.record_send(
                agent_name=self._source.name,
                target_agent=target_agent,
                invocation_id=envelope.invocation_id,
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
            if envelope.target is None:
                return AgentSendResult(
                    target_agent=target_agent, target_kind=target_kind,
                    session_id=session_id, invocation_id=normalized_invocation_id,
                    created_new_task=created_new_task,
                    error="No target address for broker delivery",
                )
            await self._broker.send_to(envelope.target, envelope.to_broker_message())

        return AgentSendResult(
            target_agent=target_agent,
            target_kind=target_kind,
            session_id=session_id,
            invocation_id=normalized_invocation_id,
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
        lines.append("Use invocation_id=null when sending to a normal agent.")
        lines.append('Use invocation_id="" when starting a new task for a subagent.')
        lines.append('Use invocation_id="<existing invocation_id>" when continuing a subagent task.')
        return "\n".join(lines)
