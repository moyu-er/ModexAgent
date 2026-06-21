"""Internal agent communication service — routing logic shared by sync and async tools.

This service owns target validation, invocation_id semantics, session ID construction,
envelope building, and delivery. Tool classes become thin wrappers around it.
"""

from __future__ import annotations

import logging
import tempfile
import uuid as _uuid_mod
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from framework.core.session_id import SessionIdFactory, SessionInfo
from framework.core.session_registry import SessionRegistry
from framework.memory.core.message import ChatMessage
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.multi_agent.template import AgentTemplate
from framework.multi_agent.template_registry import AgentTemplateRegistry
from framework.multi_agent.tools import CommunicationTarget, CommunicationTargetStore
from framework.pipeline.adapters import OutputAdapter
from framework.pipeline.snapshot import PoolDataSnapshot
from framework.tools.workspace_scoped import WorkspaceRootProvider
from framework.workspace.resources import WorkspaceResources

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.messaging.broker import MessageBroker
    from framework.multi_agent.address import AgentAddress
    from framework.multi_agent.bus import AgentMessageBus
    from framework.multi_agent.comm_tracker import CommunicationTracker
    from framework.multi_agent.pool import AgentPool
    from framework.multi_agent.registry import AgentRegistry


class WorkspaceManager(ABC):
    """Abstract interface for workspace resolution used by AgentCommunicationService.

    The concrete implementation (e.g. ``WorkspaceResolverCell``) is provided
    by the bot layer.  This ABC keeps the framework decoupled from business
    types while giving the constructor a precise type annotation.
    """

    @abstractmethod
    def resolve_workspace(self) -> WorkspaceResources:
        """Return the currently active workspace's resources.

        The returned :class:`WorkspaceResources` exposes ``pool_data`` (pool
        name → :class:`PoolDataSnapshot`), whose values carry ``memory_dir``,
        ``runtime_dir``, and ``pruned_manager``.
        """
        ...


logger = logging.getLogger(__name__)

_TASK_ID_BYTES = 8

# ── Fork context file registry — tracks persisted fork XML files for cleanup ──
# Key: subagent session_id, Value: path to fork XML file
_FORK_FILE_REGISTRY: dict[str, Path] = {}


def cleanup_fork_context(session_id: str) -> None:
    """Delete the persisted fork context file for a session, if one exists.

    Called by AgentPool during session eviction. Safe to call for sessions
    that have no fork context (no-op).
    """
    fork_file = _FORK_FILE_REGISTRY.pop(session_id, None)
    if fork_file is not None and fork_file.exists():
        try:
            fork_file.unlink()
            logger.debug("Fork context file cleaned: %s", fork_file)
        except OSError:
            pass


async def _load_per_agent_mcp(
    tool_manager: Any,
    mcp_json: Path,
    agent_name: str,
) -> None:
    """Load MCP servers from a per-agent JSON file and register as tools."""
    import json

    from framework.ioc.configs.app import _resolve_env_in
    from framework.tools.mcp import MCPClientManager
    from framework.tools.mcp_adapter import MCPToolAdapter
    from framework.tools.registry import ToolRegistry

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


def _messages_to_xml(messages: list[ChatMessage], parent_name: str) -> str:
    """Convert ChatMessage list to XML for system-prompt injection."""
    lines = [
        f'<forked_context source="{parent_name}">',
        f"  <info>Inherited {len(messages)} messages from parent session.</info>",
    ]
    for i, msg in enumerate(messages):
        role = msg.role
        content = msg.content or ""
        content_str = str(content)[:2000]
        name_attr = ""
        if role == "tool" and msg.name:
            name_attr = f' name="{msg.name}"'
        lines.append(f'  <message index="{i}" role="{role}"{name_attr}>')
        lines.append(f"    <![CDATA[{content_str}]]>")
        lines.append("  </message>")
    lines.append("</forked_context>")
    return "\n".join(lines)


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
        session_factory: SessionIdFactory | None = None,
        session_registry: SessionRegistry | None = None,
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
        inbox_consumer: Any | None = None,
        notification_service: Any | None = None,
        main_agent_name: str | None = None,
        pruned_manager: Any | None = None,
        target_store: CommunicationTargetStore | None = None,
        runtime_dir: Path | None = None,
        # ── Injection points for bot-layer customization ──
        output_adapter_factory: Callable[[], OutputAdapter] | None = None,
        on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None,
        # ── Workspace integration (Unit E) ──
        # When both are set, ``_memory_dir`` / ``_runtime_dir`` / ``_pruned_manager``
        # are resolved per-call from the active Workspace's pool_data instead of
        # the fixed ctor args. The fixed args remain as fallbacks (used by tests
        # and any non-workspace wiring).
        # DEPRECATED: workspace_manager is superseded by root_provider for path
        # resolution.  Retained only for pool_data store resolution (memory_dir,
        # runtime_dir, pruned_manager) which has no replacement yet.
        workspace_manager: WorkspaceManager | None = None,
        root_provider: WorkspaceRootProvider | None = None,
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
        self._memory_dir = memory_dir
        self._safety = safety
        self._pool_llm_model = pool_llm_model
        self._pool_llm_temperature = pool_llm_temperature
        self._pool_llm_max_tokens = pool_llm_max_tokens
        self._inbox_consumer = inbox_consumer
        self._notification_service = notification_service
        self._main_agent_name = main_agent_name
        self._pruned_manager = pruned_manager
        self._target_store = target_store
        self._runtime_dir = runtime_dir
        self._output_adapter_factory = output_adapter_factory
        self._on_subagent_created = on_subagent_created
        self._workspace_manager = workspace_manager
        self._root_provider = root_provider
        self._fallback_output_root: Path | None = None

    def _resolve_source(self, context: AgentContext) -> AgentAddress:
        """Resolve effective source address from context, fallback to constructor default."""
        if context.session.agent_name:
            return AgentAddress(name=context.session.agent_name)
        return self._source

    # ------------------------------------------------------------------
    # Workspace-aware path resolution (Unit E)
    # ------------------------------------------------------------------

    def _resolve_pool_data(self) -> PoolDataSnapshot | None:
        """Return the active Workspace's PoolData snapshot for this pool, or None.

        When a workspace_manager is wired AND it has an active workspace AND
        that workspace has built pool_data for ``_pool_name``, return it.
        Otherwise return None so callers fall back to the fixed ctor args.
        """
        mgr = self._workspace_manager
        if mgr is None or self._pool_name is None:
            return None
        try:
            ws = mgr.resolve_workspace()
        except RuntimeError:
            return None
        if ws is None:
            return None
        return ws.pool_data.get(self._pool_name)

    def _resolved_memory_dir(self) -> Path | None:
        """memory_dir: prefer the active workspace's pool_data, else ctor arg."""
        pool_data = self._resolve_pool_data()
        if pool_data is not None:
            return pool_data.memory_dir
        return self._memory_dir

    def _resolved_runtime_dir(self) -> Path | None:
        """runtime_dir: prefer the active workspace's pool_data, else ctor arg."""
        pool_data = self._resolve_pool_data()
        if pool_data is not None:
            return pool_data.runtime_dir
        return self._runtime_dir

    def _resolve_output_root(self) -> Path:
        """Resolved runtime_dir, NEVER the process CWD.

        Subagent OUTPUT.md is written under this dir; silently defaulting to the
        process CWD (``Path('.')``) would collide across workspaces and leak
        outside the owning workspace. When neither workspace pool_data nor a
        constructor runtime_dir is available (broken wiring / isolated unit
        tests), fall back to a single isolated temp dir per service and warn —
        never the shared process CWD.
        """
        runtime_dir = self._resolved_runtime_dir()
        if runtime_dir is not None:
            return runtime_dir
        if self._fallback_output_root is None:
            self._fallback_output_root = Path(tempfile.mkdtemp(prefix="modex-subagent-"))
            logger.warning(
                "Subagent runtime_dir unresolved; writing OUTPUT.md to isolated "
                "temp dir %s. Materialize the workspace to write into it.",
                self._fallback_output_root,
            )
        return self._fallback_output_root

    def _resolved_pruned_manager(self) -> Any | None:
        """pruned_manager: prefer the active workspace's pool_data, else ctor arg."""
        pool_data = self._resolve_pool_data()
        if pool_data is not None:
            return pool_data.pruned_manager
        return self._pruned_manager

    def _fork_workspace(self) -> Path | None:
        """Return the fork context workspace directory, or None if unavailable.

        Never synthesizes a path under ``project_dir`` — that would write outside
        the owning workspace. Returns None when no workspace memory dir is resolved.
        """
        return self._resolved_memory_dir()

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

    def _ensure_invocation(
        self,
        target_agent: str,
        parent_session_id: SessionInfo | str,
        invocation_id: str | None,
        target_kind: AgentCommKind | None,
    ) -> tuple[str | None, None, Path | None]:
        """Ensure invocation_id and create output dir for subagent targets.

        Returns (invocation_id, None, output_path).  Returns (invocation_id, None, None)
        when target is not a subagent.  Trace dirs are no longer created here —
        the workspace-rooted ``JsonFileTraceStore`` creates them on first write
        via ``TraceCollectorHook``, uniformly for ALL agents (no subagent distinction).
        """
        if target_kind != AgentCommKind.SUBAGENT:
            return invocation_id, None, None

        # Generate or validate invocation_id
        if not invocation_id or str(invocation_id).lower() == "null":
            invocation_id = _uuid_mod.uuid4().hex[:_TASK_ID_BYTES]
        else:
            # Check if output already exists (collision-avoidance).
            existing_sid = self._session_factory.create(
                agent_name=target_agent,
                parent_session_id=parent_session_id,
                external_id=invocation_id,
            )
            existing_session = str(existing_sid)
            runtime_dir = self._resolved_runtime_dir()
            if runtime_dir is not None:
                existing_output = runtime_dir / "output" / existing_session
                if not existing_output.exists():
                    invocation_id = _uuid_mod.uuid4().hex[:_TASK_ID_BYTES]

        target_sid = self._session_factory.create_with_prefix(
            agent_name=target_agent,
            prefix=invocation_id,
            parent_session_id=parent_session_id,
        )
        session_id = str(target_sid)

        runtime_dir = self._resolve_output_root()
        output_path = runtime_dir / "output" / session_id / "OUTPUT.md"

        output_path.parent.mkdir(parents=True, exist_ok=True)

        return invocation_id, None, output_path

    async def _create_dynamic_subagent(
        self,
        template: AgentTemplate,
        parent_session_id: SessionInfo | str,
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

        # ── Ensure invocation_id and create trace/output directories ──
        _inv_id, _trace_dir, _output_path = self._ensure_invocation(
            target_agent=name,
            parent_session_id=parent_session_id,
            invocation_id=invocation_id,
            target_kind=AgentCommKind.SUBAGENT,
        )
        # Use the validated/generated invocation_id
        invocation_id = _inv_id or invocation_id

        # ── Dynamic parent — the agent that dispatched this subagent ──
        parent_name = (source or self._source).name

        # Load system prompt from agents/{agent_type}.md (same convention as resolve_system_prompt)
        from framework.ioc.factories.descriptors import DEFAULT_SYSTEM_PROMPT

        system_prompt = ""
        if self._project_dir is not None:
            md_path = self._project_dir / "agents" / f"{template.agent_type}.md"
            if md_path.exists():
                system_prompt = md_path.read_text(encoding="utf-8")
        if not system_prompt:
            system_prompt = DEFAULT_SYSTEM_PROMPT

        # ── Append mode: concat parent prompt before subagent prompt ──
        from framework.tools.presets import SystemPromptMode, ToolPreset

        if template.system_prompt_mode == SystemPromptMode.APPEND:
            parent_prompt = ""
            parent_name_for_append = parent_name
            if self._pool is not None:
                parent_instance = self._pool.get(parent_name_for_append)
                if (
                    parent_instance is not None
                    and parent_instance.descriptor.system_prompt_template
                ):
                    parent_prompt = parent_instance.descriptor.system_prompt_template
            if parent_prompt:
                system_prompt = parent_prompt + "\n\n---\n\n" + system_prompt

        # ── OUTPUT.md base dir — passed to MemorySystemContextManager so
        # OutputMdProvider can compute the correct per-session path at turn time.
        # No static injection into system_prompt — the path is always fresh. ──
        runtime_dir_resolved = self._resolved_runtime_dir()
        output_base_dir: Path | None = None
        if runtime_dir_resolved is not None:
            output_base_dir = runtime_dir_resolved / "output"

        # ── Read-only guard: remind agent not to modify project files ──
        if template.tool_preset == ToolPreset.READ_ONLY:
            guard = (
                "\n\n---\n\n"
                "## Read-Only Mode\n\n"
                "You are in read-only mode for project files. Your task is to read, "
                "search, and analyze — NOT to modify project source code.\n\n"
                "- Your `write` and `edit` tools are restricted to the output directory "
                "(for writing OUTPUT.md) — they will NOT work on project paths.\n"
                "- Your `bash` tool is for reading/searching only. Do NOT use it to "
                "modify, delete, or create files.\n"
                "- Do NOT use shell redirection (> / >>) or heredocs to write files.\n"
                "- **You CAN and MUST use `write` to save OUTPUT.md** — "
                "the path shown above is in your allowed write directory."
            )
            system_prompt = system_prompt + guard

        # ── Memory: session-scoped, no knowledge layer ──
        from framework.ioc.factories.descriptors import build_session_only_memory
        from framework.memory.core.scope import MemoryAgentRole

        memory_workspace = self._resolved_memory_dir() or (
            self._project_dir / "data" / "memory" / self._pool_name
            if self._project_dir and self._pool_name
            else Path(".")
        )
        subagent_ctx = build_session_only_memory(
            cfg=template.memory,
            workspace=memory_workspace,
            agent_id=name,
            agent_role=MemoryAgentRole.SUBAGENT,
            system_prompt=system_prompt,
            pruned_manager=self._resolved_pruned_manager(),
            output_base_dir=output_base_dir,
        )

        # ── Fork context: two-stage truncation → XML → persist → system prompt ──
        from framework.memory.core.scope import MemoryContext
        from framework.tools.presets import ContextMode

        if template.context_mode == ContextMode.FORK and self._project_dir is not None:
            fork_workspace = self._fork_workspace()
            if fork_workspace is None:
                logger.warning(
                    "Fork context: no workspace available for %s, skipping injection",
                    name,
                )
            else:
                fork_file = fork_workspace / "fork_contexts" / f"{name}_{invocation_id}.xml"

                if fork_file.exists():
                    # ── Resume: load persisted fork context ──
                    fork_xml = fork_file.read_text(encoding="utf-8")
                    logger.info(
                        "Fork context: loaded persisted file for %s/%s",
                        name,
                        invocation_id,
                    )
                else:
                    # ── Initial creation: two-stage truncate + persist ──
                    fork_xml = (
                        f'<forked_context source="{parent_name}">'
                        f"  <info>No parent messages available.</info>"
                        f"</forked_context>"
                    )
                    try:
                        parent_sid_str = str(parent_session_id)
                        parent_ctx = MemoryContext(session_id=parent_sid_str)

                        # Read parent messages via abstract API
                        subagent_memory = subagent_ctx.memory_system
                        if subagent_memory is not None:
                            parent_messages = await subagent_memory.get_history(
                                parent_ctx,
                                max_messages=10000,
                            )

                            if parent_messages:
                                # Stage 1: count-based truncation
                                fork_max = template.fork_max_messages
                                truncated = parent_messages[-fork_max:]

                                # Stage 2: lossy governance (on kept messages)
                                if (
                                    template.memory is not None
                                    and template.memory.governance is not None
                                    and template.memory.governance.lossy_compaction is not None
                                ):
                                    from framework.memory.context_governance import (
                                        LossyContentCompactionGovernance,
                                    )

                                    lc = template.memory.governance.lossy_compaction
                                    governor = LossyContentCompactionGovernance(
                                        tool_result_head_chars=lc.tool_result_head_chars,
                                        assistant_head_chars=lc.assistant_head_chars,
                                        agent_head_chars=lc.agent_head_chars,
                                        user_head_chars=lc.user_head_chars,
                                        tool_args_head_chars=lc.tool_args_head_chars,
                                    )

                                    msg_dicts: list[dict[str, Any]] = [
                                        m.model_dump() for m in truncated
                                    ]
                                    compacted = await governor.apply(msg_dicts)
                                    truncated = [ChatMessage(**m) for m in compacted]

                                # Format as XML
                                fork_xml = _messages_to_xml(truncated, parent_name)

                        else:
                            logger.warning(
                                "Fork context: context manager lacks memory_system attribute, "
                                "fork context will be empty for %s",
                                name,
                            )

                        # Persist
                        fork_file.parent.mkdir(parents=True, exist_ok=True)
                        fork_file.write_text(fork_xml, encoding="utf-8")
                        logger.info(
                            "Fork context: persisted for %s/%s",
                            name,
                            invocation_id,
                        )
                    except Exception:
                        logger.exception(
                            "Fork context: failed to build for %s, continuing with empty",
                            name,
                        )

                # ── Inject fork context into system prompt ──
                fork_preamble = (
                    "\n\n---\n\n"
                    "## Fork Context\n"
                    f"You are a subagent running from a fork of agent '{parent_name}'.\n"
                    "The context below is READ-ONLY reference. Do NOT continue the\n"
                    "prior conversation. Your task starts now.\n\n"
                    f"{fork_xml}"
                )
                system_prompt = system_prompt + fork_preamble

        # ── Tool manager: standard + MCP + communication ──
        subagent_tm = await self._build_subagent_tool_manager(
            template,
            agent_name=name,
            parent_name=parent_name,
        )

        # ── Skill manager ──
        subagent_sm = None
        if template.skills is not None and template.skills.roots and self._project_dir is not None:
            skill_roots = [self._project_dir / r for r in template.skills.roots]
            existing = [d for d in skill_roots if d.exists()]
            if existing:
                from framework.core.skills import (
                    DefaultSkillBuilder,
                    FileSkillSource,
                    SkillManager,
                )

                skill_source = FileSkillSource(
                    directories=existing,
                    cache=True,
                    layout="directory",
                    skill_filename="SKILL.md",
                )
                builder = DefaultSkillBuilder(base_path=self._project_dir)
                subagent_sm = SkillManager(source=skill_source, builder=builder)

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
            skill_manager=subagent_sm,
            output_adapter=(self._output_adapter_factory or NullOutputAdapter)(),
        )

        # ── Build child session and record parent-child relationship ──
        child_session = self._session_factory.create_with_prefix(
            agent_name=name,
            prefix=invocation_id,
            parent_session_id=parent_session_id,
        )
        session_id = str(child_session)

        # ── Register child session in registry ──
        if self._session_registry is not None:
            await self._session_registry.register(child_session)

        if self._on_subagent_created is not None:
            await self._on_subagent_created(session_id, str(parent_session_id))

        # ── Wire hooks ──
        self._wire_subagent_hooks(name, parent_name=parent_name)

        # ── Register fork context file for cleanup on session eviction ──
        if template.context_mode == ContextMode.FORK:
            _fw = self._fork_workspace()
            if _fw is not None:
                _fork_path = _fw / "fork_contexts" / f"{name}_{invocation_id}.xml"
                if _fork_path.exists():
                    _FORK_FILE_REGISTRY[session_id] = _fork_path

        effective_source = source or self._source
        from framework.multi_agent.message_xml import build_agent_message

        xml_content = build_agent_message(
            source=effective_source.name,
            invocation_id=invocation_id,
            content=content,
        )
        envelope = AgentMessageEnvelope(
            payload={"content": xml_content, "message_type": "task_request"},
            source=effective_source,
            target=AgentAddress(name=name),
            message_type="task_request",
            session_id=str(parent_session_id),
            agent_session_id=session_id,
            invocation_id=invocation_id,
        )
        await self._broker.send_to(envelope.target, envelope.to_broker_message())

        logger.info(
            "Dynamic subagent created: %s (template=%s, invocation_id=%s)",
            name,
            template.agent_type,
            invocation_id,
        )

        # Add to target store (no-op if template target already exists from init)
        if self._target_store is not None:
            self._target_store.add(
                CommunicationTarget(
                    name=name,
                    kind=AgentCommKind.SUBAGENT,
                    description=template.description,
                )
            )

        return AgentSendResult(
            target_agent=name,
            target_kind=AgentCommKind.SUBAGENT,
            session_id=session_id,
            invocation_id=invocation_id,
            created_new_task=True,
            trace_dir=_trace_dir,
            output_path=_output_path,
        )

    def _wire_subagent_hooks(self, agent_name: str, parent_name: str = "main") -> None:
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
            _add_hook(
                sub_instance.pipeline,
                InboxFlushHook(
                    consumer=self._inbox_consumer,
                    agent_name=agent_name,
                ),
            )

        if self._agent_bus is not None:
            _add_hook(
                sub_instance.pipeline,
                SubagentAutoSendHook(
                    agent_bus=self._agent_bus,
                    self_name=agent_name,
                    parent_name=parent_name,
                    runtime_dir=self._resolved_runtime_dir(),
                ),
            )

        if self._notification_service is not None:
            from framework.hook.notification import MaxIterationNotifyHook

            _add_hook(
                sub_instance.pipeline,
                MaxIterationNotifyHook(
                    notification_service=self._notification_service,
                ),
            )

    async def _build_subagent_tool_manager(
        self,
        template: AgentTemplate,
        agent_name: str,
        parent_name: str = "main",
    ):
        """Build the subagent tool manager from template configuration.

        Uses template.tool_preset to determine which tools to register.
        Subagents get standard + MCP tools only. No communication tools.
        Communication is handled automatically by SubagentAutoSendHook.
        """
        from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
        from framework.tools.presets import get_preset_tools

        tm = InMemoryToolManager(config=ToolManagerConfig())

        # Bash tool factory: use SubprocessTool for subagents (no terminal)
        from framework.tools.terminal import SubprocessTool

        def _make_bash() -> SubprocessTool:
            return SubprocessTool(timeout=300)

        # Register preset tools — pass scoped_write_dir so READ_ONLY agents
        # (scout, oracle) can still write OUTPUT.md via restricted write tool.
        scoped_write_dir: Path | None = None
        runtime_dir_resolved = self._resolved_runtime_dir()
        if runtime_dir_resolved is not None:
            scoped_write_dir = runtime_dir_resolved / "output"

        for tool in get_preset_tools(
            template.tool_preset,
            subprocess_tool_factory=_make_bash,
            scoped_write_dir=scoped_write_dir,
            root_provider=self._root_provider,
        ):
            tm.register(tool)

        # MCP tools from per-agent config file: config/mcp/{agentType}.json
        if self._project_dir is not None:
            mcp_json = self._project_dir / "config" / "mcp" / f"{template.agent_type}.json"
            if mcp_json.exists():
                try:
                    await _load_per_agent_mcp(tm, mcp_json, agent_name)
                except Exception:
                    logger.exception(
                        "Failed to load MCP tools for subagent %s from %s",
                        agent_name,
                        mcp_json,
                    )
            else:
                logger.info(
                    "Subagent %s: no MCP config at %s (agent_type=%s)",
                    agent_name, mcp_json, template.agent_type,
                )
        else:
            logger.warning(
                "Subagent %s: _project_dir is None — MCP loading skipped. "
                "Ensure project_dir is passed to AgentCommunicationService.",
                agent_name,
            )

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
            f"Task dispatched to '{target_agent}' — running in background.",
            "",
            "ⓘ  The subagent works asynchronously. You will receive an inbox",
            "notification when it finishes. Do NOT read the files below yet —",
            "they are not ready and may not exist until the subagent completes.",
            "",
        ]
        if result.invocation_id:
            lines.append(f"invocation_id: {result.invocation_id}")
        if result.trace_dir is not None:
            lines.append(f"Trace (after notification): {result.trace_dir}/operations.jsonl")
        if result.output_path is not None:
            lines.append(f"Output (after notification): {result.output_path}")
        lines.extend([
            "",
            "When the notification arrives, read the Output file for the",
            "deliverable. If the notification says the task is incomplete,",
            "use the invocation_id above to resume the subagent session.",
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
        """Core routing logic shared by sync and async sends."""
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

        # If SUBAGENT + template matched → create or resume
        if target_kind == AgentCommKind.SUBAGENT and template is not None:
            if invocation_id is None or invocation_id.strip() == "":
                # New task → create subagent with fresh UUID
                new_invocation_id = _uuid_mod.uuid4().hex[:_TASK_ID_BYTES]
                return await self._create_dynamic_subagent(
                    template=template,
                    parent_session_id=parent_sid,
                    invocation_id=new_invocation_id,
                    content=content,
                    source=effective_source,
                )
            # Concrete invocation_id → check if agent is still alive
            if self._pool is None or self._pool.get(target_agent) is None:
                # Agent was destroyed (completed or bot restarted)
                # Re-create with the same invocation_id to resume session
                return await self._create_dynamic_subagent(
                    template=template,
                    parent_session_id=parent_sid,
                    invocation_id=invocation_id,
                    content=content,
                    source=effective_source,
                )
            # Agent exists → fall through to normal inbox delivery

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

            # Ensure invocation and create trace/output directories
            resolved_invocation_id, trace_dir, output_path = self._ensure_invocation(
                target_agent=target_agent,
                parent_session_id=parent_sid,
                invocation_id=None,
                target_kind=AgentCommKind.SUBAGENT,
            )

            target_session = self._session_factory.create_with_prefix(
                agent_name=target_agent,
                prefix=resolved_invocation_id,
                parent_session_id=parent_sid,
            )
            session_id = str(target_session)
            if self._session_registry is not None:
                await self._session_registry.register(target_session)

            from framework.multi_agent.message_xml import build_agent_message

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

            # Deliver: inbox + immediate wakeup (not send_silent)
            if async_mode and self._agent_bus is not None:
                await self._agent_bus.send(session_id, envelope)
            else:
                if envelope.target is None:
                    return AgentSendResult(
                        target_agent=target_agent,
                        target_kind=target_kind,
                        session_id=session_id,
                        invocation_id=resolved_invocation_id,
                        created_new_task=True,
                        error="No target address for broker delivery",
                    )
                await self._broker.send_to(envelope.target, envelope.to_broker_message())

            return AgentSendResult(
                target_agent=target_agent,
                target_kind=target_kind,
                session_id=session_id,
                invocation_id=resolved_invocation_id,
                created_new_task=True,
                trace_dir=trace_dir,
                output_path=output_path,
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

        from framework.multi_agent.message_xml import build_agent_message

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

        # 7. Deliver
        if async_mode and self._agent_bus is not None:
            await self._agent_bus.send_silent(session_id, envelope)
        else:
            if envelope.target is None:
                return AgentSendResult(
                    target_agent=target_agent,
                    target_kind=target_kind,
                    session_id=session_id,
                    invocation_id=normalized_invocation_id,
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
