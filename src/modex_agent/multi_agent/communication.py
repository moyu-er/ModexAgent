"""Internal agent communication service — pure router (ADR-0015 D5).

This service owns target validation, invocation_id semantics, session ID
construction, envelope building, and delivery. It NEVER creates agent
instances — subagent materialization is owned by ``AgentTemplate.materialize``
(invoked lazily by the Drainer-spawner in ``AgentPool``). Fork context
construction/cleanup is owned by ``ContextForkBuilder``; workspace path
resolution by ``WorkspacePathResolver``.

Tool classes become thin wrappers around this service.
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import CommunicationTarget, CommunicationTargetStore

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent.bus import AgentMessageBus
    from modex_agent.multi_agent.comm_tracker import CommunicationTracker
    from modex_agent.multi_agent.pool import AgentPool
    from modex_agent.multi_agent.registry import AgentRegistry
    from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver


logger = logging.getLogger(__name__)

_TASK_ID_BYTES = 8


async def _load_per_agent_mcp(
    tool_manager: Any,
    mcp_json: Path,
    agent_name: str,
) -> None:
    """Load MCP servers from a per-agent JSON file and register as tools."""
    import json

    from modex_agent.ioc.configs.app import _resolve_env_in
    from modex_agent.tools.mcp import MCPClientManager
    from modex_agent.tools.mcp_adapter import MCPToolAdapter
    from modex_agent.tools.registry import ToolRegistry

    with open(mcp_json, encoding="utf-8") as f:
        raw = json.load(f)

    servers = raw.get("mcpServers") or raw.get("servers") or {}
    if not servers:
        logger.info("Agent %s: MCP config %s has no servers defined", agent_name, mcp_json.name)
        return

    logger.info(
        "Agent %s: loading MCP from %s — %d server(s): %s",
        agent_name, mcp_json.name, len(servers), list(servers.keys()),
    )
    servers = _resolve_env_in(servers)
    manager = MCPClientManager(config=servers)

    # Wrap with a hard timeout so unreachable servers never block
    # subagent creation. httpx has its own timeout, but DNS / TCP
    # handshake can still hang on some platforms.
    import asyncio as _asyncio

    try:
        await _asyncio.wait_for(manager.initialize(), timeout=15.0)
    except TimeoutError:
        logger.warning(
            "Agent %s: MCP initialization timed out after 15s for %s — "
            "server(s) %s unreachable; continuing without MCP tools",
            agent_name, mcp_json.name, list(servers.keys()),
        )
        return
    except Exception:
        logger.exception(
            "Agent %s: MCP initialization failed for %s",
            agent_name, mcp_json.name,
        )
        return

    if not manager.connected_servers:
        logger.warning(
            "Agent %s: MCP config %s — %d server(s) configured but NONE connected "
            "(check MCP_BEARER_TOKEN env var and network)",
            agent_name, mcp_json.name, len(servers),
        )
        return

    adapter = MCPToolAdapter(mcp_manager=manager, default_prefix=True, tool_timeout=60)
    registry = ToolRegistry()
    await adapter.register_tools(registry=registry)

    registered = 0
    for name in registry.list_tools():
        tool = registry.get_tool(name)
        if tool is not None:
            tool_manager.register(tool)
            registered += 1

    logger.info(
        "Agent %s: %d MCP tools loaded from %s",
        agent_name,
        registered,
        mcp_json.name,
    )


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
    trace_dir: Path | None = None
    output_path: Path | None = None


class AgentCommunicationService:
    """Internal service for inter-agent communication routing.

    Owns validation, invocation_id semantics, session ID building, envelope
    construction, and sync/async delivery selection. It is a pure router: it
    NEVER constructs agent instances (materialization is owned by
    ``AgentTemplate.materialize``, invoked lazily by the Drainer-spawner).
    Tool classes delegate to this service.
    """

    def __init__(
        self,
        source: AgentAddress,
        broker: MessageBroker,
        registry: AgentRegistry,
        *,
        agent_bus: AgentMessageBus | None = None,
        session_factory: SessionIdFactory | None = None,
        session_registry: SessionRegistry | None = None,
        comm_tracker: CommunicationTracker | None = None,
        template_registry: AgentTemplateRegistry | None = None,
        pool: AgentPool | None = None,
        pool_name: str | None = None,
        project_dir: Path | None = None,
        target_store: CommunicationTargetStore | None = None,
        workspace_path_resolver: "WorkspacePathResolver | None" = None,
    ) -> None:
        self._source = source
        self._broker = broker
        self._registry = registry
        self._agent_bus = agent_bus
        self._session_factory = session_factory or SessionIdFactory()
        self._session_registry = session_registry
        self._comm_tracker = comm_tracker
        self._template_registry = template_registry
        self._pool = pool
        self._pool_name = pool_name
        self._project_dir = project_dir
        self._target_store = target_store
        self._workspace_path_resolver = workspace_path_resolver

    def _resolve_source(self, context: AgentContext) -> AgentAddress:
        """Resolve effective source address from context, fallback to constructor default."""
        if context.session.agent_name:
            return AgentAddress(name=context.session.agent_name)
        return self._source

    def _subagent_runtime_dir(
        self, target_kind: AgentCommKind | None
    ) -> Path | None:
        """Resolved runtime_dir for SUBAGENT targets, else None."""
        if target_kind != AgentCommKind.SUBAGENT:
            return None
        if self._workspace_path_resolver is None:
            return None
        return self._workspace_path_resolver.runtime_dir()

    def _subagent_output_path(
        self, target_kind: AgentCommKind | None, session_id: str
    ) -> Path | None:
        """Compute the subagent OUTPUT.md path for the ack text (parity with main).

        Returns ``runtime_dir / output / <session_id> / OUTPUT.md`` for SUBAGENT
        targets when a workspace_path_resolver is wired, creating the directory
        so the path exists when the caller prints it. ``None`` for non-subagent
        targets or when no resolver/runtime_dir is available.
        """
        runtime_dir = self._subagent_runtime_dir(target_kind)
        if runtime_dir is None:
            return None
        output_path = runtime_dir / "output" / session_id / "OUTPUT.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return output_path

    def _subagent_trace_dir(
        self, target_kind: AgentCommKind | None, session_id: str
    ) -> Path | None:
        """Compute the subagent execution-trace dir for the ack text.

        Returns ``runtime_dir / trace / <session_id>`` for SUBAGENT targets so
        the ack can show ``Trace (after notification): <dir>/operations.jsonl``
        (main's stated intent — "includes trace/output paths"). The directory
        itself is created by the JsonFileTraceStore on first write, not here
        (parity with main's _ensure_invocation).
        """
        runtime_dir = self._subagent_runtime_dir(target_kind)
        if runtime_dir is None:
            return None
        return runtime_dir / "trace" / session_id

    def _resolve_target(
        self, target_agent: str
    ) -> tuple[AgentCommKind | None, AgentTemplate | None]:
        """Resolve target_agent to comm_kind + optional template.

        Templates are checked BEFORE the pool registry so that each new
        invocation creates a fresh agent instance with the correct
        OUTPUT.md path in its system prompt.  Already-running agents are
        discovered through the pool registry only when no template matches.
        """
        # 1. Check template registry FIRST — ensures new invocations get
        #    a fresh agent with correct OUTPUT.md path
        if self._template_registry is not None and self._pool_name is not None:
            template = self._template_registry.get_template(self._pool_name, target_agent)
            if template is not None:
                return AgentCommKind.SUBAGENT, template

        # 2. Check if registered in pool (already-running agent)
        descriptor = self._registry.get_descriptor(target_agent)
        if descriptor is not None:
            return descriptor.comm_kind, None

        profile = self._registry.get_profile(target_agent)
        if profile is not None:
            return profile.comm_kind, None

        return None, None

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

    async def send_async(
        self,
        *,
        target_agent: str,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> str:
        """Send asynchronously via inbox. Returns acknowledgement text.

        For subagent targets includes trace/output paths so the caller can
        monitor progress and read the deliverable.
        """
        result = await self._send(
            target_agent=target_agent,
            content=content,
            invocation_id=invocation_id,
            context=context,
            async_mode=True,
        )
        if result is None or result.error:
            return f"Error: {result.error if result else 'unknown'}"
        lines = [
            f"Task dispatched to '{target_agent}' - running in background.",
            "",
            "Note: the subagent works asynchronously. You will receive an inbox",
            "notification when it finishes.",
            "",
        ]
        if result.invocation_id:
            lines.append(f"invocation_id: {result.invocation_id}")
        if result.trace_dir is not None:
            lines.append(
                "Trace (live execution log, append-only, safe to read while it "
                f"runs): {result.trace_dir}/operations.jsonl"
            )
        if result.output_path is not None:
            lines.append(
                "Output (final deliverable, empty/absent until the subagent "
                f"completes): {result.output_path}"
            )
        lines.extend([
            "",
            "You may tail the Trace file at any time to follow progress. Wait for",
            "the notification before reading the Output file. If the notification",
            "says the task is incomplete, use the invocation_id above to resume.",
        ])
        return "\n".join(lines)

    async def _send(
        self,
        *,
        target_agent: str,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
        async_mode: bool,
    ) -> AgentSendResult | None:
        """Core routing logic shared by sync and async sends.

        Pure router: resolves the target, mints invocation_id + session,
        builds the envelope, and enqueues. Agent materialization happens
        lazily in the Drainer-spawner (ADR-0015 D3).
        """
        # 1. Resolve parent session from context
        parent_sid = context.session
        effective_source = self._resolve_source(context)

        # 2. Look up target
        target_kind, template = self._resolve_target(target_agent)
        if target_kind is None:
            return AgentSendResult(
                target_agent=target_agent,
                target_kind=AgentCommKind.NORMAL,
                session_id="",
                invocation_id=None,
                created_new_task=False,
                error=f"Target agent '{target_agent}' not found",
            )

        # Cold start: mint invocation_id + session, enqueue. The Drainer-spawner
        # materializes the instance on first drain (ADR-0015 D3).
        if target_kind == AgentCommKind.SUBAGENT and template is not None:
            # Subagent-to-subagent still forbidden
            if context.comm_kind == AgentCommKind.SUBAGENT:
                return AgentSendResult(
                    target_agent=target_agent,
                    target_kind=target_kind,
                    session_id="",
                    invocation_id=None,
                    created_new_task=False,
                    error="Subagents can only reply to normal agents; send subagent-to-subagent requests through a normal agent.",
                )
            if invocation_id is None or invocation_id.strip() == "":
                invocation_id = _uuid_mod.uuid4().hex[:_TASK_ID_BYTES]
            target_session = self._session_factory.create_with_prefix(
                agent_name=target_agent, prefix=invocation_id, parent_session_id=parent_sid,
            )
            session_id = str(target_session)
            if self._session_registry is not None:
                await self._session_registry.register(target_session)
            from modex_agent.multi_agent.message_xml import build_agent_message
            xml_content = build_agent_message(source=effective_source.name, invocation_id=invocation_id, content=content)
            envelope = AgentMessageEnvelope(
                payload={"content": xml_content, "message_type": "task_request"},
                source=effective_source, target=AgentAddress(name=target_agent),
                message_type="task_request", session_id=str(parent_sid),
                agent_session_id=session_id, invocation_id=invocation_id,
            )
            if self._comm_tracker is not None:
                self._comm_tracker.record_send(
                    agent_name=effective_source.name, target_agent=target_agent,
                    invocation_id=invocation_id, session_id=session_id, content_summary=content[:500],
                )
            if self._agent_bus is not None:
                await self._agent_bus.send(session_id, envelope)
            elif envelope.target is not None:
                await self._broker.send_to(envelope.target, envelope.to_broker_message())
            return AgentSendResult(
                target_agent=target_agent, target_kind=target_kind,
                session_id=session_id, invocation_id=invocation_id, created_new_task=True,
                output_path=self._subagent_output_path(target_kind, session_id),
                trace_dir=self._subagent_trace_dir(target_kind, session_id),
            )

        # ── Registered SUBAGENT new task: agent instance exists, send via inbox ──
        # When a SUBAGENT is already registered (template is None) and this is a NEW
        # task (empty invocation_id), generate a new invocation_id and deliver through
        # inbox with immediate wakeup so the consumer can process it concurrently with
        # any other active session. Continuations (existing invocation_id) fall through
        # to the original normal delivery path below.
        if (
            target_kind == AgentCommKind.SUBAGENT
            and template is None
            and (invocation_id is None or invocation_id.strip() == "")
        ):
            # Subagent-to-subagent still forbidden
            if context.comm_kind == AgentCommKind.SUBAGENT:
                return AgentSendResult(
                    target_agent=target_agent,
                    target_kind=target_kind,
                    session_id="",
                    invocation_id=None,
                    created_new_task=False,
                    error="Subagents can only reply to normal agents; send subagent-to-subagent requests through a normal agent.",
                )

            # Mint invocation_id directly (collision-avoidance deleted per ADR-0015 D6).
            resolved_invocation_id = _uuid_mod.uuid4().hex[:_TASK_ID_BYTES]

            target_session = self._session_factory.create_with_prefix(
                agent_name=target_agent,
                prefix=resolved_invocation_id,
                parent_session_id=parent_sid,
            )
            session_id = str(target_session)
            if self._session_registry is not None:
                await self._session_registry.register(target_session)

            from modex_agent.multi_agent.message_xml import build_agent_message

            xml_content = build_agent_message(
                source=effective_source.name,
                invocation_id=resolved_invocation_id,
                content=content,
            )
            envelope = AgentMessageEnvelope(
                payload={"content": xml_content, "message_type": "task_request"},
                source=effective_source,
                target=AgentAddress(name=target_agent),
                message_type="task_request",
                session_id=str(parent_sid),
                agent_session_id=session_id,
                invocation_id=resolved_invocation_id,
            )

            # Record in communication tracker
            if self._comm_tracker is not None:
                self._comm_tracker.record_send(
                    agent_name=effective_source.name,
                    target_agent=target_agent,
                    invocation_id=resolved_invocation_id,
                    session_id=session_id,
                    content_summary=content[:500],
                )

            # Deliver: ADR-0015 always use bus.send when available.
            if self._agent_bus is not None:
                await self._agent_bus.send(session_id, envelope)
            elif envelope.target is not None:
                await self._broker.send_to(envelope.target, envelope.to_broker_message())
            else:
                return AgentSendResult(
                    target_agent=target_agent,
                    target_kind=target_kind,
                    session_id=session_id,
                    invocation_id=resolved_invocation_id,
                    created_new_task=True,
                    error="No target address for broker delivery",
                )

            return AgentSendResult(
                target_agent=target_agent,
                target_kind=target_kind,
                session_id=session_id,
                invocation_id=resolved_invocation_id,
                created_new_task=True,
                output_path=self._subagent_output_path(target_kind, session_id),
                trace_dir=self._subagent_trace_dir(target_kind, session_id),
            )

        # 3. Validate invocation_id
        if (
            context.comm_kind == AgentCommKind.SUBAGENT
            and target_kind == AgentCommKind.SUBAGENT
        ):
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
                target_agent=target_agent,
                target_kind=target_kind,
                session_id="",
                invocation_id=None,
                created_new_task=False,
                error=error,
            )

        created_new_task = invocation_id == "" and target_kind == AgentCommKind.SUBAGENT

        # 4. Build session ID (receiver-owned)
        if target_kind == AgentCommKind.SUBAGENT and normalized_invocation_id is not None:
            target_session = self._session_factory.create_with_prefix(
                agent_name=target_agent,
                prefix=normalized_invocation_id,
                parent_session_id=parent_sid,
            )
        else:
            target_session = self._session_factory.create(
                agent_name=target_agent,
                parent_session_id=parent_sid,
                external_id=normalized_invocation_id,
            )
        session_id = str(target_session)

        # Register subagent session in registry so the parent→child relationship
        # is persisted to session_index — the WebUI tree depends on it.
        if target_kind == AgentCommKind.SUBAGENT and self._session_registry is not None:
            await self._session_registry.register(target_session)

        # 5. Build envelope (XML-wrapped per spec Section 4.1)
        # For subagent replying to normal parent: preserve caller's snowflake on envelope
        envelope_invocation_id = normalized_invocation_id
        if target_kind == AgentCommKind.NORMAL and context.comm_kind == AgentCommKind.SUBAGENT:
            envelope_invocation_id = parent_sid.session_id_prefix

        from modex_agent.multi_agent.message_xml import build_agent_message

        effective_source_name = effective_source.name
        xml_content = build_agent_message(
            source=effective_source_name,
            invocation_id=envelope_invocation_id,
            content=content,
        )
        envelope = AgentMessageEnvelope(
            payload={"content": xml_content, "message_type": "agent_message"},
            source=effective_source,
            target=AgentAddress(kind="agent", name=target_agent),
            message_type="agent_message",
            session_id=str(parent_sid),
            agent_session_id=session_id,
            invocation_id=envelope_invocation_id,
        )

        # 6. Record communication tracker events
        if self._comm_tracker is not None and envelope.invocation_id is not None:
            if (
                target_kind == AgentCommKind.NORMAL
                and context.comm_kind == AgentCommKind.SUBAGENT
            ):
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

        # 7. Deliver — ADR-0015: always use bus.send (signals the Drainer).
        # Broker fallback only when no agent_bus is wired (unit tests).
        if self._agent_bus is not None:
            await self._agent_bus.send(session_id, envelope)
        elif envelope.target is not None:
            await self._broker.send_to(envelope.target, envelope.to_broker_message())
        else:
            return AgentSendResult(
                target_agent=target_agent,
                target_kind=target_kind,
                session_id=session_id,
                invocation_id=normalized_invocation_id,
                created_new_task=created_new_task,
                error="No target address for broker delivery",
            )

        return AgentSendResult(
            target_agent=target_agent,
            target_kind=target_kind,
            session_id=session_id,
            invocation_id=normalized_invocation_id,
            created_new_task=created_new_task,
            output_path=self._subagent_output_path(target_kind, session_id),
            trace_dir=self._subagent_trace_dir(target_kind, session_id),
        )
