"""TurnContextBuilder — assemble the per-turn context, request, and AgentContext.

Owns the four turn-preparation responsibilities that used to live as private
methods on ``AgentPipeline``:

* ``build_turn_request``   — parse slash commands / approval actions into a
  :class:`TurnRequest` (was ``AgentPipeline._build_turn_request``).
* ``preprocess``           — sanitize content and process attachments (was
  ``_preprocess_input``).
* ``assemble``             — load context, write the user message, run the
  multi-agent builder (was ``_assemble_context``; delegates to
  :func:`modex_agent.pipeline.context_assembler.assemble_context`).
* ``build_runtime_and_context`` — build the typed :class:`AgentContext` +
  emitter for the turn (was ``_build_runtime_and_context``).

Behaviour is identical to the pre-extraction methods — pure move. The builder
holds no back-reference to ``AgentPipeline``: every dependency is injected via
the constructor and stored as ``self._<name>``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.adapters.output import OutputAdapter
    from modex_agent.commands.models import CommandHandlingResult, CommandProcessor
    from modex_agent.commands.skill import SkillResolver
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.agent import Agent, AgentContext
    from modex_agent.core.emitter import ContentEmitter
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.hook.runner import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.memory.context import ContextManager, ContextState
    from modex_agent.memory.context_governance import ContextGovernance
    from modex_agent.messaging.models import InputMessage
    from modex_agent.multi_agent import AgentDescriptor
    from modex_agent.multi_agent.router import RouteResult
    from modex_agent.multi_agent.session_tree.session_binding import (
        SessionBindingStore,
    )
    from modex_agent.pipeline.turn_context_config import (
        TurnContextConfigPipeline,
        TurnContextDescriptor,
    )
    from modex_agent.runtime.context import RuntimeContextManager
    from modex_agent.runtime.models import TurnSnapshot
    from modex_agent.runtime.store import TurnStateStore
    from modex_agent.utils.context_builder import MultiAgentContextBuilder
    from modex_graph.context import GraphContext

from modex_agent.adapters.emitter import StreamingAwareEmitter
from modex_agent.approval.response import parse_input_command
from modex_agent.commands.models import CommandContext
from modex_agent.core.agent import AgentContext
from modex_agent.core.media import Attachment, MediaStore
from modex_agent.core.session_id import SessionInfo
from modex_agent.messaging.models import ApprovalAction, OutputMessage, OutputMessageType
from modex_agent.pipeline.context_assembler import assemble_context
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.workspace.runtime import resolve_workspace_root

logger = logging.getLogger(__name__)


def _human_byte_size(size: int) -> str:
    """Render a byte count as a short human-readable size (e.g. ``2.3MB``).

    Used only by the transient attachment path-reference injection. Binary
    units (1024), one decimal, unit suffix B/KB/MB/GB — matches the inline
    ``{:.1f}MB`` style already used in :mod:`media_utils`.
    """
    if size < 1024:
        return f"{size}B"
    kb = size / 1024
    if kb < 1024:
        return f"{kb:.1f}KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f}MB"
    return f"{mb / 1024:.1f}GB"


def _attachment_reference(att: Attachment, ws_root: Path) -> str:
    """Build the transient mechanism-B path-reference line for one attachment.

    Format: ``[Attachment: <name> (<mime>, <human_size>) @ <absolute_path>]``.
    Resolves the record's workspace-relative ``path`` against the turn's bound
    workspace root (resolved once per turn by the caller and passed in) so the
    agent receives a tool-usable absolute path. Deliberately omits the
    ``attachment_id`` (a frontend/download concern the agent never needs —
    ADR-0013 §1). Transient: enters LLM history only, never transcript
    user-message content.
    """
    # An Attachment always carries a path (the ingest stage sets it from the
    # stored file). Defensively avoid handing the agent the workspace ROOT when
    # path is empty — degrade to an explicit <unknown path> marker instead of
    # pointing tools at the whole workspace.
    abs_path = (ws_root / att.path).resolve() if att.path else None
    mime = att.mime or "unknown"
    path_str = str(abs_path) if abs_path is not None else "<unknown path>"
    return f"[Attachment: {att.name} ({mime}, {_human_byte_size(att.size)}) @ {path_str}]"


@dataclass(frozen=True)
class TurnRequest:
    session_id: str
    input_msg: InputMessage
    user_content: str | None
    append_user_message: bool
    trigger_agent: bool
    approval_action: ApprovalAction | None = None
    approval_tool_call_id: str | None = None
    command_result: CommandHandlingResult | None = None


class TurnContextBuilder:
    """Build the per-turn TurnRequest, ContextState, and AgentContext.

    Constructed eagerly in ``AgentPipeline.__init__`` with all of the
    pipeline's turn-preparation dependencies. Every method is a verbatim
    move of the corresponding ``AgentPipeline`` private method, with
    ``self.X`` rewritten to ``self._X``.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        tool_manager: ToolManager,
        sanitizer: Callable[[str], str] | None,
        command_processor: CommandProcessor | None,
        skill_resolver: SkillResolver | None,
        context_builder: MultiAgentContextBuilder | None,
        agent_descriptor: AgentDescriptor | None,
        max_iterations: int,
        safety: RuntimeSafetyPolicy,
        runtime_services: AgentRuntimeServices | None,
        runtime_context_manager: RuntimeContextManager | None,
        governance: ContextGovernance | None,
        hook_runner: HookRunner | None,
        interceptor_chain: InterceptorChain | None,
        control_channel: InMemoryControlChannel | None,
        emitter_factory: Callable[..., ContentEmitter] | None,
        output_adapter: OutputAdapter,
        turn_store: TurnStateStore | None,
        registry: TurnSessionRegistry,
    ) -> None:
        self._agent = agent
        self._tool_manager = tool_manager
        self._sanitizer = sanitizer
        self._command_processor = command_processor
        self._skill_resolver = skill_resolver
        self._context_builder = context_builder
        self._agent_descriptor = agent_descriptor
        self._max_iterations = max_iterations
        self._safety = safety
        self._runtime_services = runtime_services
        self._runtime_context_manager = runtime_context_manager
        self._governance = governance
        self._hook_runner = hook_runner
        self._interceptor_chain = interceptor_chain
        self._control_channel = control_channel
        self._emitter_factory = emitter_factory
        self._output_adapter = output_adapter
        self._turn_store = turn_store
        self._registry = registry
        self._graph_context_resolver: Callable[[int], GraphContext[Any] | None] | None = None
        self._config_pipeline: TurnContextConfigPipeline | None = None
        self._session_binding_store: SessionBindingStore | None = None
        self._media_store_resolver: Callable[[], MediaStore] | None = None

    # ── Post-construction wiring (typed setters) ─────────────────────────
    #
    # The builder is a mutable runtime object (not frozen); pool_builder's
    # post-assembly wiring calls these setters to thread pool-level concerns
    # (governance, interceptor chain, runtime services, emitter factory) into
    # the builder after it has been constructed by the factory. Replaces the
    # previous direct ``builder._xxx = value`` assignment from outside the
    # class (type-safety rule 6 / rule 8).

    @property
    def governance(self) -> ContextGovernance | None:
        return self._governance

    @governance.setter
    def governance(self, value: ContextGovernance | None) -> None:
        self._governance = value

    @property
    def runtime_services(self) -> AgentRuntimeServices | None:
        return self._runtime_services

    @runtime_services.setter
    def runtime_services(self, value: AgentRuntimeServices | None) -> None:
        self._runtime_services = value

    @property
    def interceptor_chain(self) -> InterceptorChain | None:
        return self._interceptor_chain

    @interceptor_chain.setter
    def interceptor_chain(self, value: InterceptorChain | None) -> None:
        self._interceptor_chain = value

    @property
    def emitter_factory(self) -> Callable[..., ContentEmitter[Any]] | None:
        return self._emitter_factory

    @emitter_factory.setter
    def emitter_factory(self, value: Callable[..., ContentEmitter[Any]] | None) -> None:
        self._emitter_factory = value

    @property
    def graph_context_resolver(self) -> Callable[[int], GraphContext[Any] | None] | None:
        return self._graph_context_resolver

    @graph_context_resolver.setter
    def graph_context_resolver(
        self, value: Callable[[int], GraphContext[Any] | None] | None
    ) -> None:
        self._graph_context_resolver = value

    @property
    def config_pipeline(self) -> TurnContextConfigPipeline | None:
        return self._config_pipeline

    @config_pipeline.setter
    def config_pipeline(self, value: TurnContextConfigPipeline | None) -> None:
        self._config_pipeline = value

    @property
    def session_binding_store(self) -> SessionBindingStore | None:
        return self._session_binding_store

    @session_binding_store.setter
    def session_binding_store(self, value: SessionBindingStore | None) -> None:
        self._session_binding_store = value

    @property
    def media_store_resolver(self) -> Callable[[], MediaStore] | None:
        return self._media_store_resolver

    @media_store_resolver.setter
    def media_store_resolver(self, value: Callable[[], MediaStore] | None) -> None:
        self._media_store_resolver = value

    async def build_turn_request(
        self,
        input_msg: InputMessage,
        session_id: str,
        input_metadata: dict[str, Any],
        pending_snapshot: TurnSnapshot | None,
        *,
        pool_data: PoolDataSnapshot | None = None,
    ) -> TurnRequest | None:
        # Webui approval decision (structured, NOT a slash command): short-circuit
        # straight to the resume branch — no user message, no agent trigger.
        # IM /approve still goes through the command-processor path below.
        decision = input_msg.approval_decision
        if decision is not None:
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=None,
                append_user_message=False,
                trigger_agent=False,
                approval_action=decision.action,
                approval_tool_call_id=decision.tool_call_id,
            )
        if self._command_processor is None:
            parsed_command = parse_input_command(input_msg.content or "")
            approval_action = parsed_command.approval_action if parsed_command is not None else None
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=None,
                append_user_message=True,
                trigger_agent=True,
                approval_action=approval_action,
            )

        from modex_agent.commands.constants import CommandAction, CommandParseStatus

        parse_result = self._command_processor.parse(input_msg.content or "")
        if parse_result.status == CommandParseStatus.PLAIN_INPUT:
            # Plain input has no command transform to apply — leave user_content
            # None so turn_runner keeps preprocess's sanitized_content (which
            # carries the attachment path-reference injection, ADR-0013 §10).
            # Setting it to input_msg.content here made turn_runner override and
            # discard the injection, so the agent never perceived attachments.
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=None,
                append_user_message=True,
                trigger_agent=True,
            )

        command_context = CommandContext(
            session_id=session_id,
            input_msg=input_msg,
            agent_name=self._agent.name,
            skill_resolver=self._skill_resolver,
            turn_store=pool_data.turn_store if pool_data is not None else self._turn_store,
            pending_approval=pending_snapshot,
        )
        result = await self._command_processor.handle(input_msg.content or "", command_context)
        if result.notice:
            await self._output_adapter.send(
                OutputMessage(
                    content=result.notice,
                    session_id=session_id,
                    message_type=OutputMessageType.COMMAND_RESPONSE,
                ),
                session_id,
            )
        if result.action == CommandAction.NOTICE:
            return None
        if result.action == CommandAction.APPROVAL_DECISION:
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=None,
                append_user_message=False,
                trigger_agent=False,
                approval_action=result.approval_action,
                command_result=result,
            )
        if result.action in (
            CommandAction.CONTINUE_AGENT,
            CommandAction.TRANSFORM_TO_USER_INPUT,
        ):
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=result.user_content,
                append_user_message=result.append_user_message,
                trigger_agent=result.trigger_agent,
                command_result=result,
            )
        return None

    async def preprocess(
        self,
        input_msg: InputMessage,
        session_id: str,
        input_metadata: dict[str, Any],
        _route_result: RouteResult | None,
    ) -> str | None:
        """Preprocess input: sanitize content and handle attachments.

        Returns:
            sanitized_content (with mechanism-B attachment reference lines
            appended when the turn carries resolved attachments).
        """
        sanitized_content = input_msg.content
        if self._sanitizer is not None:
            sanitized_content = self._sanitizer(sanitized_content)
            if sanitized_content != input_msg.content:
                logger.info("Input content sanitized for session %s", session_id)

        # --- Mechanism B (v1, any model): transient path-reference injection. ---
        # For each gate-accepted inbound Attachment, append a text reference so the
        # agent perceives the file (name/mime/size/tool-usable absolute path) and
        # inspects it with its tools. This is TRANSIENT — it enters the agent LLM
        # history ( sanitized_content → assemble_context → context_state.history ),
        # NOT the persisted transcript user content (PersistUserMessageStage writes
        # envelope.content verbatim and carries the Attachment record separately).
        # ADR-0013 §1/§10.
        resolved = input_msg.attachments_resolved
        if resolved:
            # Resolve the workspace root once per turn (not per attachment):
            # it is identical for every record in the same turn.
            ws_root = resolve_workspace_root()
            ref_lines = [_attachment_reference(a, ws_root) for a in resolved]
            injection = "\n".join(ref_lines)
            sanitized_content = (
                f"{sanitized_content}\n{injection}" if sanitized_content else injection
            )

        return sanitized_content

    async def assemble(
        self,
        session_id: str,
        input_msg: InputMessage,
        input_metadata: dict[str, Any],
        sanitized_content: str | None,
        ctx_mgr: ContextManager,
        route_result: RouteResult | None,
        _is_approval_cmd: bool,
        append_user_message: bool = True,
    ) -> ContextState:
        """Assemble context state via context_assembler module."""
        caps = self._runtime_services.model_info if self._runtime_services is not None else None
        return await assemble_context(
            session_id,
            input_msg,
            input_metadata,
            sanitized_content,
            ctx_mgr,
            route_result,
            _is_approval_cmd,
            agent_descriptor=self._agent_descriptor,
            tool_manager=self._tool_manager,
            context_builder=self._context_builder,
            append_user_message=append_user_message,
            model_info=caps,
        )

    def build_runtime_and_context(
        self,
        session: SessionInfo,
        context_state: ContextState,
        ctx_mgr: ContextManager,
        *,
        input_metadata: dict[str, Any] | None = None,
        pool_data: PoolDataSnapshot | None = None,
        workspace: Path | None = None,
        turn_descriptor: TurnContextDescriptor | None = None,
    ) -> tuple[AgentContext, ContentEmitter]:
        """Build AgentContext and emitter for the turn.
        """

        # Ensure per-session injection queue exists
        self._registry.get_or_create_queue(session.session_id)

        # ---- typed TurnIdentity (new) ----
        from uuid import uuid4

        from modex_agent.runtime.models import TurnIdentity

        agent_id = (
            self._agent_descriptor.address.name
            if self._agent_descriptor is not None
            else self._agent.name
        )
        turn_identity = TurnIdentity(
            agent_id=agent_id,
            session=session,
            turn_id=uuid4().hex,
        )

        agent_context = AgentContext(
            system_prompt=context_state.system_prompt,
            history=context_state.history,
            tool_manager=self._tool_manager,
            session=session,
            comm_kind=self._agent_descriptor.comm_kind if self._agent_descriptor else None,
            max_iterations=self._max_iterations,
        )
        agent_context.system_prompt_pipeline = context_state.system_prompt_pipeline
        agent_context.identity = turn_identity
        # Per-turn snapshot (opaque PoolData) for hooks/agents that need
        # the resolved workspace's stores (e.g. experience dir). None when
        # no workspace manager is wired.
        agent_context.workspace_snapshot = pool_data
        agent_context.workspace = workspace

        # ---- governance (pending injection, etc.) — unconditional ----
        base_services = self._runtime_services
        base_gov = base_services.governance if base_services is not None else None
        governance = ctx_mgr.wrap_governance(base_gov or self._governance, session.session_id)

        # Resolve the turn-scoped turn store. Precedence:
        # process-scope runtime_services override > per-turn pool snapshot
        # > pipeline-level self.turn_store.
        snapshot_turn_store = pool_data.turn_store if pool_data is not None else self._turn_store
        snapshot_trace_store = pool_data.trace_store if pool_data is not None else None
        media_store = base_services.media_store if base_services is not None else None
        if self._media_store_resolver is not None and (
            snapshot_turn_store is not None or governance is not None
        ):
            media_store = self._media_store_resolver()

        # ---- typed AgentRuntime with ReActTurnState (new) ----
        if snapshot_turn_store is not None:
            from modex_agent.agents.react.state import ReActTurnState
            from modex_agent.runtime.enums import AgentKind, TurnCustomKey
            from modex_agent.runtime.enums import TurnPhase as RTurnPhase
            from modex_agent.runtime.services import AgentRuntime

            react_state = ReActTurnState(
                identity=turn_identity,
                agent_kind=AgentKind.REACT,
                phase=RTurnPhase.CREATED,
            )
            services = AgentRuntimeServices(
                hooks=(base_services.hooks if base_services is not None else None)
                or self._hook_runner,
                interceptors=(base_services.interceptors if base_services is not None else None)
                or self._interceptor_chain,
                approval=base_services.approval if base_services is not None else None,
                governance=governance,
                turn_store=(
                    base_services.turn_store
                    if base_services is not None and base_services.turn_store is not None
                    else snapshot_turn_store
                ),
                trace_store=snapshot_trace_store,
                pending_input_queue=self._registry.get_queue(session.session_id),
                safety=base_services.safety if base_services is not None else self._safety,
                runtime_context_manager=(
                    base_services.runtime_context_manager
                    if base_services is not None
                    and base_services.runtime_context_manager is not None
                    else self._runtime_context_manager
                ),
                control_channel=self._control_channel
                or (base_services.control_channel if base_services is not None else None),
                model_info=(base_services.model_info if base_services is not None else None),
                media_store=media_store,
            )
            agent_context.runtime = AgentRuntime(services=services, state=react_state)
            agent_context.runtime.state.custom[TurnCustomKey.MAX_TOOLS_PER_TURN] = None
            agent_context.runtime.state.custom[TurnCustomKey.MAX_TURNS] = 3
        elif governance is not None:
            # Lightweight runtime for governance-only mode (no turn_store)
            from modex_agent.agents.react.state import ReActTurnState
            from modex_agent.runtime.enums import AgentKind
            from modex_agent.runtime.enums import TurnPhase as RTurnPhase
            from modex_agent.runtime.services import AgentRuntime

            agent_context.runtime = AgentRuntime(
                services=AgentRuntimeServices(
                    governance=governance,
                    trace_store=snapshot_trace_store,
                    control_channel=self._control_channel
                    or (base_services.control_channel if base_services is not None else None),
                    model_info=(base_services.model_info if base_services is not None else None),
                    media_store=media_store,
                ),
                state=ReActTurnState(
                    identity=turn_identity,
                    agent_kind=AgentKind.REACT,
                    phase=RTurnPhase.CREATED,
                ),
            )

        # Trace linkage metadata for the turn's spans (subagent parentage).
        if agent_context.runtime is not None and input_metadata is not None:
            from modex_agent.runtime.enums import TurnCustomKey

            trace_id = input_metadata.get("trace_id")
            if trace_id is not None:
                agent_context.runtime.state.custom[TurnCustomKey.TRACE_ID] = str(trace_id)
            parent_span_id = input_metadata.get("parent_span_id")
            if parent_span_id is not None:
                agent_context.runtime.state.custom[TurnCustomKey.PARENT_SPAN_ID] = str(
                    parent_span_id
                )

        # Emitter selection
        if self._emitter_factory:
            emitter = self._emitter_factory(session.session_id)
        else:
            emitter = StreamingAwareEmitter(
                output_adapter=self._output_adapter,
                session_id=session.session_id,
                send_timeout=self._safety.turn.output_send_timeout_seconds,
            )

        if turn_descriptor is not None and self._config_pipeline is not None:
            self._config_pipeline.configure(agent_context, turn_descriptor)

        return agent_context, emitter
