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
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import CommunicationTargetStore, resolve_parent_name

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent.bus import AgentMessageBus
    from modex_agent.multi_agent.comm_tracker import CommunicationTracker
    from modex_agent.multi_agent.pool import AgentPool
    from modex_agent.multi_agent.registry import AgentRegistry
    from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver
    from modex_agent.tools.mcp.registry import McpConnectionRegistry


logger = logging.getLogger(__name__)

_TASK_ID_BYTES = 8


async def _load_per_agent_mcp(
    tool_manager: Any,
    selection: list[str],
    project_dir: Path,
    agent_name: str,
    *,
    registry: McpConnectionRegistry | None = None,
) -> None:
    """Resolve an agent's MCP ``selection`` against the registry and register tools.

    Reads ``<project_dir>/config/mcp/registry.json`` (Claude-style
    ``{"mcpServers": {...}}``), keeps only the servers named in
    ``selection``, applies ``${ENV}`` interpolation, connects, and registers
    the adapted tools on ``tool_manager``. Failures are logged and swallowed
    so subagent creation is never blocked by an unreachable MCP server.

    When ``registry`` is provided (ADR-0017 shared-connection overlay), the
    registry.json read and private ``MCPClientManager`` are bypassed: a
    :class:`SharedMcpBackend` is obtained via ``registry.acquire(selection)``
    instead. ``project_dir`` is then unused for MCP (it stays in the signature
    for the non-registry path and caller compatibility).
    """
    import json

    from modex_agent.ioc.configs.app import _resolve_env_in
    from modex_agent.tools.mcp import MCPClientManager
    from modex_agent.tools.mcp_adapter import acquire_mcp_tools

    if not selection:
        return

    if registry is not None:
        # Shared-connection path: the registry owns connection lifecycle and
        # already knows the server configs, so registry.json is not read.
        try:
            backend = await registry.acquire(selection)
        except Exception:
            logger.exception(
                "Agent %s: shared MCP acquire failed; continuing without MCP tools",
                agent_name,
            )
            return

        tools = await acquire_mcp_tools(backend, tool_timeout=60)
        for tool in tools:
            tool_manager.register(tool)

        logger.info(
            "Agent %s: %d MCP tools loaded from selection %s",
            agent_name,
            len(tools),
            selection,
        )
        return

    registry_path = project_dir / "config" / "mcp" / "registry.json"
    if not registry_path.exists():
        logger.info("Agent %s: MCP registry %s missing; skipping MCP tools", agent_name, registry_path)
        return

    with open(registry_path, encoding="utf-8") as f:
        raw = json.load(f)

    # Fail-soft (framework) vs fail-loud (bot). The bot path
    # (``bot.config.mcp_registry.resolve_agent_mcp_servers``) raises
    # ``UnknownMcpServer`` for stale/typo'd selections at pool build; here we
    # only warn and drop unknown names so a subagent still materializes — a
    # bad MCP selection must never block subagent construction (the framework
    # runs under arbitrary business wiring, including stale YAML during tests).
    all_servers = raw.get("mcpServers") or raw.get("servers") or {}
    missing = [s for s in selection if s not in all_servers]
    if missing:
        logger.warning(
            "Agent %s: MCP servers not in registry %s: %s",
            agent_name, registry_path.name, missing,
        )
        return

    servers = {name: all_servers[name] for name in selection}
    logger.info(
        "Agent %s: loading MCP from %s — %d server(s): %s",
        agent_name, registry_path.name, len(servers), list(servers.keys()),
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
            agent_name, registry_path.name, list(servers.keys()),
        )
        return
    except Exception:
        logger.exception(
            "Agent %s: MCP initialization failed for %s",
            agent_name, registry_path.name,
        )
        return

    if not manager.connected_servers:
        logger.warning(
            "Agent %s: MCP config %s — %d server(s) configured but NONE connected "
            "(check MCP_BEARER_TOKEN env var and network)",
            agent_name, registry_path.name, len(servers),
        )
        return

    adapter_tools = await acquire_mcp_tools(manager, tool_timeout=60)
    for tool in adapter_tools:
        tool_manager.register(tool)

    logger.info(
        "Agent %s: %d MCP tools loaded from selection %s",
        agent_name,
        len(adapter_tools),
        selection,
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
        itself is created by the JsonFileTraceStore on first write, not here.
        """
        runtime_dir = self._subagent_runtime_dir(target_kind)
        if runtime_dir is None:
            return None
        return runtime_dir / "trace" / session_id

    def _resolve_target(self, target_agent: str) -> AgentCommKind | None:
        """Resolve target_agent to its comm kind, or None if unknown.

        A template match (template registry) classifies the target as a
        SUBAGENT even before it is registered; otherwise the pool registry's
        descriptor/profile supplies the kind. ``template`` is intentionally
        NOT returned: the router does not branch on template presence —
        materialization is the InboxPoller's job, decided from the live
        registry (``pool.get``), not from the send-side lookup.
        """
        if self._template_registry is not None and self._pool_name is not None:
            if self._template_registry.get_template(self._pool_name, target_agent) is not None:
                return AgentCommKind.SUBAGENT
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

    @staticmethod
    def _star_topology_error(
        context: "AgentContext",
        target_kind: AgentCommKind | None,
        target_agent: str,
    ) -> str | None:
        """Star-topology policy gate. Returns an error string if the send is
        forbidden, else None.

        A subagent may only address its parent (a NORMAL agent); both
        subagent→subagent and subagent→non-parent-NORMAL are rejected. The
        parent is recovered from ``context.session.parent_session_id`` (the
        production poller-driven path populates it) via ``resolve_parent_name``;
        when it is unavailable (legacy/fallback) the defense is best-effort
        and the send is allowed. This is the single enforcement point
        (ADR-0015 D4/D8) — the send trunk never re-checks topology.
        """
        if context.comm_kind != AgentCommKind.SUBAGENT:
            return None
        if target_kind == AgentCommKind.SUBAGENT:
            return (
                "Subagents can only reply to normal agents; send subagent-to-"
                "subagent requests through a normal agent."
            )
        # Subagent → NORMAL: must be the resolved parent. Best-effort when the
        # parent cannot be recovered (no parent_session_id on legacy paths).
        parent_name = resolve_parent_name(context)
        if parent_name is not None and target_agent != parent_name:
            return (
                f"Subagents can only address the agent that assigned their task "
                f"({parent_name!r}); routing to other normal agents "
                f"({target_agent!r}) is not allowed. Send the request through "
                f"your parent."
            )
        return None

    async def _deliver(self, envelope: AgentMessageEnvelope) -> str | None:
        """Single delivery path: ``bus.send`` when an agent bus is wired,
        else ``broker.send_to`` fallback (unit tests / no-bus wiring). Returns
        an error string only when neither path is available.
        """
        if self._agent_bus is not None:
            await self._agent_bus.send(envelope.agent_session_id, envelope)
            return None
        if envelope.target is not None:
            await self._broker.send_to(envelope.target, envelope.to_broker_message())
            return None
        return "No target address for broker delivery"

    @staticmethod
    def _error_result(
        target_agent: str,
        target_kind: AgentCommKind,
        error: str,
        *,
        session_id: str = "",
        invocation_id: str | None = None,
    ) -> AgentSendResult:
        """Build a failed-send result. Every error return from ``_send`` is a
        not-created task, so the common fields collapse here."""
        return AgentSendResult(
            target_agent=target_agent,
            target_kind=target_kind,
            session_id=session_id,
            invocation_id=invocation_id,
            created_new_task=False,
            error=error,
        )

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
        builds the envelope, and enqueues via ``_deliver``. Agent
        materialization happens lazily in the InboxPoller (ADR-0015 D3).

        Branches on ``target_kind`` only — template presence is irrelevant to
        the send logic (the poller decides materialization from the live
        registry). Star topology is enforced once, up front.
        """
        parent_sid = context.session
        effective_source = self._resolve_source(context)

        # 1. Resolve target kind.
        target_kind = self._resolve_target(target_agent)
        if target_kind is None:
            return self._error_result(
                target_agent, AgentCommKind.NORMAL,
                f"Target agent '{target_agent}' not found",
            )

        # 2. Star topology: a subagent may only address its parent (a NORMAL).
        if (topo_err := self._star_topology_error(context, target_kind, target_agent)) is not None:
            return self._error_result(target_agent, target_kind, topo_err)

        # 3. Normalize invocation_id (NORMAL → None; SUBAGENT → mint if empty).
        normalized_invocation_id, verror = self._validate_invocation_id(
            invocation_id, target_kind
        )
        if verror is not None:
            return self._error_result(target_agent, target_kind, verror)
        created_new_task = target_kind == AgentCommKind.SUBAGENT and (
            invocation_id is None or invocation_id.strip() == ""
        )

        from modex_agent.multi_agent.message_xml import build_agent_message

        # 4. SUBAGENT target — task-scoped session keyed by invocation_id.
        # The poller materializes the instance on first turn (ADR-0015 D3).
        if target_kind == AgentCommKind.SUBAGENT:
            target_session = self._session_factory.create_with_prefix(
                agent_name=target_agent,
                prefix=normalized_invocation_id,
                parent_session_id=parent_sid,
            )
            session_id = str(target_session)
            if self._session_registry is not None:
                await self._session_registry.register(target_session)
            xml_content = build_agent_message(
                source=effective_source.name,
                invocation_id=normalized_invocation_id,
                content=content,
            )
            envelope = AgentMessageEnvelope(
                payload={"content": xml_content, "message_type": AgentMessageType.TASK_REQUEST},
                source=effective_source,
                target=AgentAddress(name=target_agent),
                message_type=AgentMessageType.TASK_REQUEST,
                session_id=str(parent_sid),
                agent_session_id=session_id,
                invocation_id=normalized_invocation_id,
            )
            if self._comm_tracker is not None:
                self._comm_tracker.record_send(
                    agent_name=effective_source.name,
                    target_agent=target_agent,
                    invocation_id=normalized_invocation_id,
                    session_id=session_id,
                    content_summary=content[:500],
                )
            deliver_err = await self._deliver(envelope)
            if deliver_err is not None:
                return self._error_result(
                    target_agent, target_kind, deliver_err,
                    session_id=session_id, invocation_id=normalized_invocation_id,
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

        # 5. NORMAL target — one stable receiver session per conversation.
        # A subagent consulting/replying to its parent MUST route to the
        # parent's ACTUAL session (``context.session.parent_session_id``), the
        # same key ``SubagentAutoSendHook`` uses. Minting a fresh session here
        # (the previous generic-NORMAL path) created a phantom duplicate main
        # session on every consult — the real parent never saw the message and
        # the phantom then re-resumed the subagent by invocation_id, orphaning
        # it. Only the main→main / no-parent fallback mints a new session.
        if (
            context.comm_kind == AgentCommKind.SUBAGENT
            and context.session.parent_session_id
        ):
            target_session = SessionInfo.from_str(context.session.parent_session_id)
        else:
            target_session = self._session_factory.create(
                agent_name=target_agent,
                parent_session_id=parent_sid,
                external_id=normalized_invocation_id,
            )
        session_id = str(target_session)

        envelope_invocation_id = normalized_invocation_id
        if context.comm_kind == AgentCommKind.SUBAGENT:
            envelope_invocation_id = parent_sid.session_id_prefix

        xml_content = build_agent_message(
            source=effective_source.name,
            invocation_id=envelope_invocation_id,
            content=content,
        )
        envelope = AgentMessageEnvelope(
            payload={"content": xml_content, "message_type": AgentMessageType.AGENT_MESSAGE},
            source=effective_source,
            target=AgentAddress(kind="agent", name=target_agent),
            message_type=AgentMessageType.AGENT_MESSAGE,
            session_id=str(parent_sid),
            agent_session_id=session_id,
            invocation_id=envelope_invocation_id,
        )

        if self._comm_tracker is not None and envelope.invocation_id is not None:
            if context.comm_kind == AgentCommKind.SUBAGENT:
                # Subagent→parent reply: close the pending-send bracket.
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

        deliver_err = await self._deliver(envelope)
        if deliver_err is not None:
            return self._error_result(
                target_agent, target_kind, deliver_err,
                session_id=session_id, invocation_id=normalized_invocation_id,
            )
        # NORMAL targets carry no trace/output paths (only SUBAGENT acks do).
        return AgentSendResult(
            target_agent=target_agent,
            target_kind=target_kind,
            session_id=session_id,
            invocation_id=normalized_invocation_id,
            created_new_task=created_new_task,
        )
