"""Internal agent communication service — routing logic shared by sync and async tools.

This service owns target validation, invocation_id semantics, session ID construction,
envelope building, and delivery. Tool classes become thin wrappers around it.
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from framework.multi_agent.address import AgentAddress
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.multi_agent.session_id import DefaultSessionIdStrategy
from framework.multi_agent.template import AgentTemplate
from framework.multi_agent.template_registry import AgentTemplateRegistry

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.messaging.broker import MessageBroker
    from framework.multi_agent.address import AgentAddress
    from framework.multi_agent.bus import AgentMessageBus
    from framework.multi_agent.comm_tracker import CommunicationTracker
    from framework.multi_agent.pool import AgentPool
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
    warning: str | None = None


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
        template_registry: AgentTemplateRegistry | None = None,
        pool: AgentPool | None = None,
        pool_name: str | None = None,
        project_dir: Path | None = None,
    ) -> None:
        self._source = source
        self._broker = broker
        self._registry = registry
        self._agent_bus = agent_bus
        self._session_strategy = session_strategy or DefaultSessionIdStrategy()
        self._comm_tracker = comm_tracker
        self._template_registry = template_registry
        self._pool = pool
        self._pool_name = pool_name
        self._project_dir = project_dir

    def _resolve_target(self, target_agent: str) -> tuple[AgentCommKind | None, AgentTemplate | None]:
        """Resolve target_agent to comm_kind + optional template."""
        # 1. Check if registered in registry (AgentPool or AgentDirectory)
        descriptor = self._registry.get_descriptor(target_agent)
        if descriptor is not None:
            return descriptor.comm_kind, None

        profile = self._registry.get_profile(target_agent)
        if profile is not None:
            return profile.comm_kind, None

        # 2. Check if it's a template type name
        if self._template_registry is not None and self._pool_name is not None:
            template = self._template_registry.get_template(self._pool_name, target_agent)
            if template is not None:
                return AgentCommKind.SUBAGENT, template

        return None, None

    async def _create_dynamic_subagent(
        self,
        template: AgentTemplate,
        conversation_id: str,
        invocation_id: str,
        content: str,
    ) -> AgentSendResult:
        """Create a dynamic subagent from template and send initial task."""
        if self._pool is None:
            return AgentSendResult(
                target_agent=template.agent_type,
                target_kind=AgentCommKind.SUBAGENT,
                session_id="",
                invocation_id=None,
                created_new_task=False,
                error="AgentPool not available for dynamic creation",
            )

        name = f"dyn.{template.agent_type}.{_uuid_mod.uuid4().hex[:8]}"

        # Load system prompt from agents/{pool_name}/{agent_type}.md
        system_prompt = ""
        if self._project_dir is not None and self._pool_name is not None:
            md_path = self._project_dir / "agents" / self._pool_name / f"{template.agent_type}.md"
            if md_path.exists():
                system_prompt = md_path.read_text(encoding="utf-8")

        # Build AgentDescriptor from template
        from framework.multi_agent.descriptor import AgentDescriptor, AgentLLMConfig

        descriptor = AgentDescriptor(
            address=AgentAddress(name=name, comm_kind=AgentCommKind.SUBAGENT),
            llm_config=AgentLLMConfig(),
            system_prompt_template=system_prompt,
            max_iterations=template.max_steps,
            execution_strategy="react",
            context_strategy="persistent",
        )

        # Register in pool (pool handles memory, tool manager, etc.)
        await self._pool.register_resident(descriptor)

        # Build session ID for the new subagent
        session_id = self._session_strategy.format(
            conversation_id=conversation_id,
            agent_name=name,
            invocation_id=invocation_id,
        )

        # Send initial task via broker
        envelope = AgentMessageEnvelope(
            payload={"content": content, "message_type": "task_request"},
            source=self._source,
            target=AgentAddress(name=name),
            message_type="task_request",
            conversation_id=conversation_id,
            agent_session_id=session_id,
            invocation_id=invocation_id,
        )
        await self._broker.send_to(envelope.target, envelope.to_broker_message())

        logger.info(
            "Dynamic subagent created: %s (template=%s, invocation_id=%s)",
            name, template.agent_type, invocation_id,
        )

        return AgentSendResult(
            target_agent=name,
            target_kind=AgentCommKind.SUBAGENT,
            session_id=session_id,
            invocation_id=invocation_id,
            created_new_task=True,
        )

    def _validate_invocation_id(
        self,
        invocation_id_in: str | None,
        target_kind: AgentCommKind,
    ) -> tuple[str | None, str | None]:
        """Validate invocation_id against target kind. Returns (normalized_invocation_id, error).

        Rules:
        - NORMAL target: invocation_id must be None.
        - SUBAGENT target: invocation_id must not be None. "" generates a new id.
        """
        if target_kind == AgentCommKind.NORMAL:
            if invocation_id_in is not None:
                return None, f"Cannot send with invocation_id to a normal agent ({target_kind.value})"
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
        text = f"Message sent to {result.target_agent}." + (
            f" invocation_id: {result.invocation_id}" if result.invocation_id else ""
        )
        if result.warning:
            text = f"{text} {result.warning}"
        return text

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
        text = f"Message sent to {result.target_agent}." + (
            f" invocation_id: {result.invocation_id}" if result.invocation_id else ""
        )
        if result.warning:
            text = f"{text} {result.warning}"
        return text

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
        target_kind, template = self._resolve_target(target_agent)
        if target_kind is None:
            return AgentSendResult(
                target_agent=target_agent, target_kind=AgentCommKind.NORMAL,
                session_id="", invocation_id=None, created_new_task=False,
                error=f"Target agent '{target_agent}' not found",
            )

        # If SUBAGENT + template matched + empty invocation_id → create new
        if target_kind == AgentCommKind.SUBAGENT and template is not None:
            if invocation_id is None or invocation_id.strip() == "":
                new_invocation_id = _uuid_mod.uuid4().hex[:_TASK_ID_BYTES]
                return await self._create_dynamic_subagent(
                    template=template,
                    conversation_id=conversation_id,
                    invocation_id=new_invocation_id,
                    content=content,
                )

        # 3. Validate invocation_id
        warning = None
        if session_meta.comm_kind == AgentCommKind.SUBAGENT and target_kind == AgentCommKind.SUBAGENT:
            return AgentSendResult(
                target_agent=target_agent,
                target_kind=target_kind,
                session_id="",
                invocation_id=None,
                created_new_task=False,
                error="Subagents can only reply to normal agents; send subagent-to-subagent requests through a normal agent.",
            )
        if target_kind == AgentCommKind.NORMAL and invocation_id is not None:
            warning = "invocation_id was ignored for normal-agent delivery; do not pass invocation_id when targeting normal agents."
            invocation_id = None
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
        # For subagent replying to normal parent: preserve caller's invocation_id on envelope
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
            if target_kind == AgentCommKind.NORMAL and session_meta.comm_kind == AgentCommKind.SUBAGENT:
                self._comm_tracker.acknowledge(
                    invocation_id=envelope.invocation_id,
                    reply_from=self._source.name,
                    reply_summary=content[:500],
                )
                self._comm_tracker.acknowledge_received(
                    invocation_id=envelope.invocation_id,
                    owner_agent=self._source.name,
                    reply_to=target_agent,
                    reply_summary=content[:500],
                )
            else:
                self._comm_tracker.record_send(
                    agent_name=self._source.name,
                    target_agent=target_agent,
                    invocation_id=envelope.invocation_id,
                    session_id=session_id,
                    content_summary=content[:500],
                )

        # 7. Deliver
        if async_mode and self._agent_bus is not None:
            await self._agent_bus.send_silent(session_id, envelope)
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
            warning=warning,
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
