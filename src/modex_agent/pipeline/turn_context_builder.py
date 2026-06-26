"""TurnContextBuilder — assemble the per-turn context, request, and AgentContext.

Owns the four turn-preparation responsibilities that used to live as private
methods on ``AgentPipeline``:

* ``build_turn_request``   — parse slash commands / approval actions into a
  :class:`TurnRequest` (was ``AgentPipeline._build_turn_request``).
* ``preprocess``           — sanitize content, process attachments, apply the
  route prompt modifier (was ``_preprocess_input``).
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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.agent import Agent, AgentContext
    from modex_agent.core.context import ContextManager, ContextState
    from modex_agent.core.emitter import ContentEmitter
    from modex_agent.core.runtime_context import RuntimeContextManager
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.core.types import InputMessage
    from modex_agent.hook.runner import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.memory.context_governance import ContextGovernance
    from modex_agent.multi_agent import AgentDescriptor
    from modex_agent.multi_agent.router import RouteResult
    from modex_agent.runtime.store import TurnStateStore
    from modex_agent.utils.context_builder import MultiAgentContextBuilder
    from modex_agent.utils.media_utils import MediaBlock, MediaProcessor

    from modex_agent.commands.models import CommandHandlingResult, CommandProcessor
    from modex_agent.core.skills import SkillManager
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.pipeline.adapters import OutputAdapter
    from modex_agent.runtime.models import TurnSnapshot

from modex_agent.approval.response import parse_input_command
from modex_agent.approval.types import ApprovalAction
from modex_agent.commands.models import CommandContext
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import StreamingAwareEmitter
from modex_agent.core.session_id import SessionInfo
from modex_agent.pipeline.adapters import OutputMessage
from modex_agent.pipeline.context_assembler import assemble_context
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.services import AgentRuntimeServices

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnRequest:
    session_id: str
    input_msg: InputMessage
    user_content: str | None
    append_user_message: bool
    trigger_agent: bool
    approval_action: ApprovalAction | None = None
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
        skill_manager: SkillManager | None,
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
        self._skill_manager = skill_manager
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

    async def build_turn_request(
        self,
        input_msg: InputMessage,
        session_id: str,
        input_metadata: dict[str, Any],
        pending_snapshot: TurnSnapshot | None,
        *,
        pool_data: PoolDataSnapshot | None = None,
    ) -> TurnRequest | None:
        if self._command_processor is None:
            parsed_command = parse_input_command(input_msg.content or "")
            approval_action = parsed_command.approval_action if parsed_command is not None else None
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=input_msg.content,
                append_user_message=True,
                trigger_agent=True,
                approval_action=approval_action,
            )

        from modex_agent.commands.constants import CommandAction, CommandParseStatus

        parse_result = self._command_processor.parse(input_msg.content or "")
        if parse_result.status == CommandParseStatus.PLAIN_INPUT:
            return TurnRequest(
                session_id=session_id,
                input_msg=input_msg,
                user_content=input_msg.content,
                append_user_message=True,
                trigger_agent=True,
            )

        command_context = CommandContext(
            session_id=session_id,
            input_msg=input_msg,
            agent_name=self._agent.name,
            skill_manager=self._skill_manager,
            turn_store=pool_data.turn_store if pool_data is not None else self._turn_store,
            pending_approval=pending_snapshot,
        )
        result = await self._command_processor.handle(input_msg.content or "", command_context)
        if result.notice:
            await self._output_adapter.send(
                OutputMessage(
                    content=result.notice,
                    session_id=session_id,
                    message_type="command_response",
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
        route_result: RouteResult | None,
    ) -> tuple[str | None, list[MediaBlock], MediaProcessor | None]:
        """Preprocess input: sanitize, handle attachments, apply route modifier.

        Returns:
            (sanitized_content, media_blocks, media_processor).
        """
        sanitized_content = input_msg.content
        if self._sanitizer is not None:
            sanitized_content = self._sanitizer(sanitized_content)
            if sanitized_content != input_msg.content:
                logger.info("Input content sanitized for session %s", session_id)

        # 处理附件（通用媒体类型，不限于图片）
        attachments = getattr(input_msg, "attachments", None) or []
        media_blocks: list[MediaBlock] = []
        _media_processor: MediaProcessor | None = None
        if attachments:
            try:
                from modex_agent.utils.media_utils import MediaProcessor

                _media_processor = MediaProcessor()
                media_result = await _media_processor.process(attachments)
                if media_result.document_text:
                    sanitized_content = (
                        f"{sanitized_content}\n\n{media_result.document_text}".strip()
                        if sanitized_content
                        else media_result.document_text
                    )
                media_blocks = media_result.media_blocks
            except Exception as e:
                logger.warning("Attachment processing failed for session %s: %s", session_id, e)

        # 应用路由的 prompt modifier（agent 消息跳过，前缀由 to_messages() 统一注入）
        source_agent = input_metadata.get("source_agent")
        if not source_agent and route_result and route_result.prompt_modifier:
            sanitized_content = route_result.prompt_modifier + sanitized_content

        return sanitized_content, media_blocks, _media_processor

    async def assemble(
        self,
        session_id: str,
        input_msg: InputMessage,
        input_metadata: dict[str, Any],
        sanitized_content: str | None,
        media_blocks: list[MediaBlock],
        _media_processor: MediaProcessor | None,
        ctx_mgr: ContextManager,
        route_result: RouteResult | None,
        _is_approval_cmd: bool,
        append_user_message: bool = True,
    ) -> ContextState:
        """Assemble context state via context_assembler module."""
        return await assemble_context(
            session_id,
            input_msg,
            input_metadata,
            sanitized_content,
            media_blocks,
            _media_processor,
            ctx_mgr,
            route_result,
            _is_approval_cmd,
            agent_descriptor=self._agent_descriptor,
            tool_manager=self._tool_manager,
            skill_manager=self._skill_manager,
            context_builder=self._context_builder,
            append_user_message=append_user_message,
        )

    def build_runtime_and_context(
        self,
        session: SessionInfo,
        context_state: ContextState,
        ctx_mgr: ContextManager,
        *,
        input_metadata: dict[str, Any] | None = None,
        pool_data: PoolDataSnapshot | None = None,
    ) -> tuple[AgentContext, ContentEmitter]:
        """Build AgentContext and emitter for the turn."""

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

        # ---- governance (pending injection, etc.) — unconditional ----
        base_services = self._runtime_services
        base_gov = base_services.governance if base_services is not None else None
        governance = ctx_mgr.wrap_governance(base_gov or self._governance, session.session_id)

        # Resolve the turn-scoped turn store. Precedence:
        # process-scope runtime_services override > per-turn pool snapshot
        # > pipeline-level self.turn_store.
        snapshot_turn_store = (
            pool_data.turn_store if pool_data is not None else self._turn_store
        )
        snapshot_trace_store = (
            pool_data.trace_store if pool_data is not None else None
        )

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
                hooks=base_services.hooks if base_services is not None else self._hook_runner,
                interceptors=(
                    base_services.interceptors
                    if base_services is not None
                    else self._interceptor_chain
                ),
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
            )
            agent_context.runtime = AgentRuntime(services=services, state=react_state)
            agent_context.runtime.state.custom[TurnCustomKey.MAX_TOOLS_PER_TURN] = None
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
                ),
                state=ReActTurnState(
                    identity=turn_identity,
                    agent_kind=AgentKind.REACT,
                    phase=RTurnPhase.CREATED,
                ),
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

        return agent_context, emitter
