"""Internal agent communication service — routing logic shared by sync and async tools.

This service owns target validation, invocation_id semantics, session ID construction,
envelope building, and delivery. Tool classes become thin wrappers around it.
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
        # Subagent-creation dependencies
        memory_dir: Path | None = None,
        safety: Any = None,
        pool_llm_model: str | None = None,
        pool_llm_temperature: float = 0.7,
        pool_llm_max_tokens: int | None = None,
        mcp_manager: Any | None = None,
        inbox_consumer: Any | None = None,
        notification_service: Any | None = None,
        main_agent_name: str | None = None,
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
        self._memory_dir = memory_dir
        self._safety = safety
        self._pool_llm_model = pool_llm_model
        self._pool_llm_temperature = pool_llm_temperature
        self._pool_llm_max_tokens = pool_llm_max_tokens
        self._mcp_manager = mcp_manager
        self._inbox_consumer = inbox_consumer
        self._notification_service = notification_service
        self._main_agent_name = main_agent_name

    def _resolve_source(self, context: AgentContext) -> AgentAddress:
        """Resolve effective source address from context, fallback to constructor default."""
        meta = context.session_meta
        if meta is not None and meta.agent_name:
            return AgentAddress(name=meta.agent_name)
        return self._source

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
        source: AgentAddress | None = None,
    ) -> AgentSendResult:
        """Create a dynamic subagent from template and send initial task.

        Builds a proper MemorySystemContextManager with session-scoped memory
        (no knowledge layer), standard tools, MCP tools, communication tools,
        and wires SubagentAutoSendHook + InboxFlushHook on the pipeline.
        """
        if self._pool is None:
            return AgentSendResult(
                target_agent=template.agent_type,
                target_kind=AgentCommKind.SUBAGENT,
                session_id="",
                invocation_id=None,
                created_new_task=False,
                error="AgentPool not available for dynamic creation",
            )

        name = template.agent_type

        # Load system prompt from agents/{pool_name}/{agent_type}.md
        from framework.ioc.factories.descriptors import DEFAULT_SYSTEM_PROMPT

        system_prompt = ""
        if self._project_dir is not None and self._pool_name is not None:
            md_path = self._project_dir / "agents" / self._pool_name / f"{template.agent_type}.md"
            if md_path.exists():
                system_prompt = md_path.read_text(encoding="utf-8")
        if not system_prompt:
            system_prompt = DEFAULT_SYSTEM_PROMPT

        # ── Memory: session-scoped, no knowledge layer ──
        from framework.ioc.factories.descriptors import build_session_only_memory
        from framework.memory.core.scope import MemoryAgentRole

        memory_workspace = self._memory_dir or (
            self._project_dir / "data" / "memory" / self._pool_name
            if self._project_dir and self._pool_name else Path(".")
        )
        subagent_ctx = build_session_only_memory(
            cfg=template.memory,
            workspace=memory_workspace,
            agent_id=name,
            agent_role=MemoryAgentRole.SUBAGENT,
            system_prompt=system_prompt,
        )

        # ── Tool manager: standard + MCP + communication ──
        subagent_tm = await self._build_subagent_tool_manager(template, agent_name=name)

        # ── Descriptor ──
        from framework.multi_agent.descriptor import AgentDescriptor, AgentLLMConfig

        descriptor = AgentDescriptor(
            address=AgentAddress(name=name),
            llm_config=AgentLLMConfig(
                model=self._pool_llm_model or "",
                temperature=self._pool_llm_temperature,
                max_tokens=self._pool_llm_max_tokens,
            ),
            system_prompt_template=system_prompt,
            max_iterations=template.max_steps,
            max_tools_per_turn=10,
            execution_strategy="react",
            context_strategy="persistent",
            safety_policy=self._safety,
            comm_kind=AgentCommKind.SUBAGENT,
        )

        # ── Register ──
        from framework.pipeline.adapters import NullOutputAdapter

        await self._pool.register_resident(
            descriptor,
            context_manager=subagent_ctx,
            tool_manager=subagent_tm,
            output_adapter=NullOutputAdapter(),
        )

        # ── Wire hooks ──
        self._wire_subagent_hooks(name)

        # ── Send initial task ──
        session_id = self._session_strategy.format(
            conversation_id=conversation_id,
            agent_name=name,
            invocation_id=invocation_id,
        )

        effective_source = source or self._source
        envelope = AgentMessageEnvelope(
            payload={"content": content, "message_type": "task_request"},
            source=effective_source,
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

    def _wire_subagent_hooks(self, agent_name: str) -> None:
        """Wire standard subagent hooks on the registered agent's pipeline."""
        if self._pool is None:
            return
        sub_instance = self._pool.get(agent_name)
        if sub_instance is None or sub_instance.pipeline is None:
            return

        from framework.hook import HookErrorPolicy, HookSpec
        from framework.hook.builtin import InboxFlushHook, SubagentAutoSendHook

        def _add_hook(pipeline: Any, hook: Any) -> None:
            if pipeline.hook_runner is not None:
                pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
            else:
                pipeline.hooks.append(hook)

        if self._inbox_consumer is not None:
            _add_hook(sub_instance.pipeline, InboxFlushHook(
                consumer=self._inbox_consumer, agent_name=agent_name,
            ))

        if self._agent_bus is not None:
            _add_hook(sub_instance.pipeline, SubagentAutoSendHook(
                agent_bus=self._agent_bus,
                self_name=agent_name,
                parent_name=self._main_agent_name or "main",
                notification_service=self._notification_service,
            ))

    async def _build_subagent_tool_manager(self, template: AgentTemplate, agent_name: str):
        """Build the subagent tool manager from template configuration.

        Includes:
        - Standard tools (file + shell + search) if template.standard_tools
        - MCP tools filtered by template.mcp_filter
        - Communication tools (send_to_agent, list_communication_targets) — always
        """
        from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
        from framework.multi_agent.tools import (
            ListCommunicationTargetsTool,
            SendToAgentTool,
        )
        from framework.multi_agent.address import AgentAddress

        tm = InMemoryToolManager(config=ToolManagerConfig(
            max_workers=10, enable_parallel=True, parallel_max_workers=5,
        ))

        # Standard tools (file + shell + search)
        if template.standard_tools:
            from framework.tools.standard import (
                EditFileTool, FindFilesTool, ListDirTool,
                ReadFileTool, SearchFilesTool, WriteFileTool,
            )
            from framework.tools.terminal import SubprocessTool
            for tool in [
                ReadFileTool(), WriteFileTool(), EditFileTool(),
                ListDirTool(), SearchFilesTool(), FindFilesTool(),
                SubprocessTool(timeout=60),
            ]:
                tm.register(tool)

        # MCP tools from template.mcp_filter
        if template.mcp_filter and self._mcp_manager is not None:
            try:
                for server_name in template.mcp_filter:
                    client = self._mcp_manager.get_client(server_name)
                    if client is not None:
                        server_tools = await client.list_tools()
                        for tool in server_tools:
                            tm.register(tool)
            except Exception:
                logger.exception(
                    "Failed to load MCP tools for subagent %s (filter=%s)",
                    agent_name, template.mcp_filter,
                )

        # Communication tools — always included so subagent can reply to parent
        subagent_address = AgentAddress(name=agent_name)
        tm.register(SendToAgentTool(
            source=subagent_address,
            broker=self._broker,
            registry=self._registry,
            agent_bus=self._agent_bus,
            service=self,
            comm_tracker=self._comm_tracker,
        ))
        tm.register(ListCommunicationTargetsTool(
            self_address=subagent_address,
            registry=self._registry,
            template_registry=self._template_registry,
            pool_name=self._pool_name,
        ))

        return tm

    def _validate_invocation_id(
        self,
        invocation_id_in: str | None,
        target_kind: AgentCommKind,
    ) -> tuple[str | None, str | None]:
        """Validate invocation_id against target kind. Returns (normalized_invocation_id, error).

        Rules:
        - NORMAL target: invocation_id is always ignored (returns None).
        - SUBAGENT target: None/empty → auto-generate; concrete value → continue session.
        """
        if target_kind == AgentCommKind.NORMAL:
            return None, None

        if target_kind == AgentCommKind.SUBAGENT:
            if invocation_id_in is None or invocation_id_in.strip() == "":
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
        if result is None or result.error:
            return f"Error: {result.error if result else 'unknown'}"
        text = f"Message sent to {target_agent}." + (
            f" invocation_id: {result.invocation_id}" if result.invocation_id else ""
        )
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
        if result is None or result.error:
            return f"Error: {result.error if result else 'unknown'}"
        text = f"Message sent to {target_agent}." + (
            f" invocation_id: {result.invocation_id}" if result.invocation_id else ""
        )
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
        effective_source = self._resolve_source(context)

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
                    source=effective_source,
                )

        # 3. Validate invocation_id
        if session_meta.comm_kind == AgentCommKind.SUBAGENT and target_kind == AgentCommKind.SUBAGENT:
            return AgentSendResult(
                target_agent=target_agent,
                target_kind=target_kind,
                session_id="",
                invocation_id=None,
                created_new_task=False,
                error="Subagents can only reply to normal agents; send subagent-to-subagent requests through a normal agent.",
            )
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
            source=effective_source,
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
                    reply_from=effective_source.name,
                    reply_summary=content[:500],
                )
                self._comm_tracker.acknowledge_received(
                    invocation_id=envelope.invocation_id,
                    owner_agent=effective_source.name,
                    reply_to=target_agent,
                    reply_summary=content[:500],
                )
            else:
                self._comm_tracker.record_send(
                    agent_name=effective_source.name,
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
