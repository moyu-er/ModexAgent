"""AgentPipeline - 端到端流程编排

提供 AgentPipeline 类，统一编排完整的输入→处理→输出流程。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.context import ContextState
    from modex_agent.core.emitter import ContentEmitter
    from modex_agent.hook.abc import HookSpec
    from modex_agent.hook.runner import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.multi_agent.router import RouteResult
    from modex_agent.workspace import WorkspaceManager
    from modex_agent.runtime.store import RuntimeCommandStore, TurnStateStore
    from modex_agent.utils.media_utils import MediaBlock, MediaProcessor

from modex_agent.commands.models import (
    CommandContext,
    CommandHandlingResult,
    CommandProcessor,
)
from modex_agent.core.agent_runtime_config import BusyInputMode
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.skills import SkillManager

from ..agents.react.state import ReActSnapshotPolicy
from ..approval.constants import ApprovalDecision
from ..approval.response import parse_input_command
from ..approval.types import ApprovalAction
from ..approval.ui import ApprovalUserInterface
from ..control.exceptions import AgentControlError
from ..core.agent import Agent, AgentContext
from ..core.context import ContextManager
from ..core.session_id import SessionInfo
from ..core.emitter import AgentResult, StreamingAwareEmitter
from ..core.graph.interrupt import GraphInterrupt
from ..core.runtime_context import RuntimeContextManager
from ..core.tool_manager import ToolManager
from ..core.types import InputMessage
from ..memory.context_governance import ContextGovernance
from ..memory import MemoryContext
from ..memory.consolidation import DreamEngine
from ..memory.history import (
    inject_attachments_to_history,
)
from ..multi_agent import AgentDescriptor
from ..multi_agent.router import AgentMessageRouter
from ..runtime.dream_locks import _dream_locks
from ..runtime.enums import SnapshotReason, TurnCustomKey, TurnPhase
from ..runtime.models import StateQueryScope, TurnSnapshot
from ..runtime.services import AgentRuntimeServices
from ..utils.context_builder import MultiAgentContextBuilder
from ..utils.deduplicator import MessageDeduplicator
from .adapters import InputAdapter, OutputAdapter, OutputMessage
from .approval_renderer import ApprovalRenderer, format_approval_prompt
from .context_assembler import assemble_context
from .snapshot import PoolDataSnapshot

logger = logging.getLogger(__name__)

_UNSET = object()

_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model or runtime error.]"


@dataclass(frozen=True)
class TurnRequest:
    session_id: str
    input_msg: InputMessage
    user_content: str | None
    append_user_message: bool
    trigger_agent: bool
    approval_action: ApprovalAction | None = None
    command_result: CommandHandlingResult | None = None


async def _safe_flush(ctx_mgr: Any, session_id: str, *, timeout: float) -> None:
    """Memory flush 带 timeout。"""
    try:
        await asyncio.wait_for(ctx_mgr.flush(session_id), timeout=timeout)
    except TimeoutError:
        logger.error("Memory flush timeout for %s", session_id)
    except Exception:
        logger.exception("Memory flush failed for %s", session_id)


class AgentPipeline:
    """Agent 流水线 - 编排完整的端到端流程

    支持多种输入源 → Agent → 多种输出源
    支持流式和非流式两种输出模式

    流程：
    1. InputAdapter 接收输入
    2. ContextManager 加载/构建上下文
    3. Agent 执行推理
    4. Emitter 分发输出事件
    5. OutputAdapter 发送到目标
    6. ContextManager 保存结果
    """

    def __init__(
        self,
        agent: Agent,
        context_manager: ContextManager,
        tool_manager: ToolManager,
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        emitter_factory: Callable[..., ContentEmitter] | None = None,
        dream_engine: DreamEngine | None = None,
        dream_interval: float | None = None,
        max_iterations: int = 10,
        incremental_flush: bool = True,
        skill_manager: SkillManager | None = None,
        hooks: list[HookSpec] | None = None,
        router: AgentMessageRouter | None = None,
        deduplicator: MessageDeduplicator | None = None,
        context_builder: MultiAgentContextBuilder | None = None,
        agent_descriptor: AgentDescriptor | None = None,
        sanitizer: Callable[[str], str] | object = _UNSET,
        context_manager_factory: Callable[..., ContextManager] | None = None,
        on_session_start: Callable[[str], None] | None = None,
        on_session_end: Callable[[str], None] | None = None,
        runtime_context_manager: RuntimeContextManager | None = None,
        governance: ContextGovernance | None = None,
        safety: RuntimeSafetyPolicy | None = None,
        hook_runner: HookRunner | None = None,
        interceptor_chain: InterceptorChain | None = None,
        control_channel: InMemoryControlChannel | None = None,
        busy_input_mode: BusyInputMode = BusyInputMode.QUEUE,
        user_interface: ApprovalUserInterface | None = None,
        turn_store: TurnStateStore | None = None,
        command_store: RuntimeCommandStore | None = None,
        runtime_services: AgentRuntimeServices | None = None,
        command_processor: CommandProcessor | None = None,
        workspace_manager: WorkspaceManager | None = None,
        pool_name: str | None = None,
        pool_data_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        """
        Args:
            ...
            safety: P0-a 运行时安全策略（timeout、retry 等），None 则使用默认
            user_interface: 审批通知 UI 接口（CLI/IM/Noop），None 则不通知
            turn_store: TurnStateStore — typed turn snapshot persistence
            command_store: RuntimeCommandStore — durable command queue
            runtime_services: process-scope services copied into each turn runtime
            workspace_manager: optional WorkspaceManager; when set together
                with ``pool_name`` each turn resolves its per-turn stores
                (context manager / turn store / command store) from the
                active workspace's PoolData snapshot instead of ``self``.
            pool_name: name of the pool whose PoolData snapshot backs each
                turn when ``workspace_manager`` is wired.
        """
        if sanitizer is _UNSET:
            from modex_agent.utils.sanitizer import ContentSanitizer

            sanitizer = ContentSanitizer.sanitize
        self.agent = agent
        self.context_manager = context_manager
        self.tool_manager = tool_manager
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter
        self.emitter_factory = emitter_factory
        self.dream_engine = dream_engine
        self.dream_interval = dream_interval
        self.max_iterations = max_iterations
        self.incremental_flush = incremental_flush
        self.skill_manager = skill_manager
        self.hooks = list(hooks) if hooks else []

        self.router = router
        self.deduplicator = deduplicator
        self.context_builder = context_builder
        self.agent_descriptor = agent_descriptor
        self.sanitizer = sanitizer
        self.context_manager_factory = context_manager_factory
        self.on_session_start = on_session_start
        self.on_session_end = on_session_end
        self.runtime_context_manager = runtime_context_manager
        self.governance = governance
        self.safety = safety or RuntimeSafetyPolicy()
        self.hook_runner = hook_runner
        self.interceptor_chain = interceptor_chain
        self.control_channel = control_channel
        self.busy_input_mode = busy_input_mode
        self.turn_store = turn_store
        self.command_store = command_store
        self.runtime_services = runtime_services
        self.command_processor = command_processor
        self.workspace_manager = workspace_manager
        self.pool_name = pool_name
        self.pool_data_resolver = pool_data_resolver
        self._approval = ApprovalRenderer(
            agent=agent,
            user_interface=user_interface,
            on_drain=self._process_message,
        )
        self._running = False
        self._dream_task: asyncio.Task | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_tasks: dict[str, asyncio.Task] = {}
        self._injection_queues: dict[str, asyncio.Queue[str]] = {}
        self._turn_uuids: dict[str, str] = {}

    @property
    def _user_interface(self):  # delegates to renderer so pool injection reaches handle()
        return self._approval._user_interface

    @_user_interface.setter
    def _user_interface(self, value):
        self._approval._user_interface = value

    def _resolve_pool_data(
        self, session_id: str = ""
    ) -> PoolDataSnapshot | None:
        """Resolve the per-turn data snapshot from the active workspace.

        When ``pool_data_resolver`` is set it takes precedence: the callable
        receives *session_id* and returns the pool name, so the pipeline
        follows per‑session pool routing (PoolSessionStore) instead of the
        static ``pool_name`` assigned at pipeline creation.  This keeps a
        session's memory, trace, and turn/command stores consistently in the
        same pool, even when pool routing changes between turns.

        When no resolver is wired the old static ``pool_name`` path is used
        (backward‑compatible).
        """
        if self.workspace_manager is None:
            return None
        ws = self.workspace_manager.resolve_workspace()

        if self.pool_data_resolver is not None and session_id:
            pool_name = self.pool_data_resolver(session_id)
            if pool_name is not None:
                return ws.pool_data.get(pool_name)
            return None

        if self.pool_name is None:
            return None
        return ws.pool_data.get(self.pool_name)

    def _is_subagent(self) -> bool:
        """Whether this pipeline backs a subagent (vs the pool's main agent)."""
        from modex_agent.core import AgentCommKind

        return (
            self.agent_descriptor is not None
            and self.agent_descriptor.comm_kind == AgentCommKind.SUBAGENT
        )

    async def run(self) -> None:
        """运行流水线"""
        self._running = True
        await self.input_adapter.start()
        await self.tool_manager.startup()

        if (
            self.dream_engine is not None
            and self.dream_interval is not None
            and self.dream_interval > 0
        ):
            self._dream_task = asyncio.create_task(self._dream_scan_loop())

        try:
            async for input_msg in self.input_adapter.receive():
                if not self._running:
                    break

                try:
                    await self._process_message(input_msg)
                except GraphInterrupt:
                    # Defense in depth: if GraphInterrupt somehow escapes
                    # _process_message_locked, propagate it rather than
                    # swallowing as a generic error.
                    raise
                except AgentControlError:
                    # Controlled exit (e.g. /stop via control side-channel) —
                    # not a failure; silently pass through.
                    # Send confirmation that the agent has actually stopped,
                    # so the user knows the /stop took effect (complementing
                    # the producer's ack sent when the command was queued).
                    try:
                        await self.output_adapter.send(
                            OutputMessage(
                                content="⏹ Agent has stopped.",
                                session_id=str(input_msg.session),
                            ),
                            str(input_msg.session),
                        )
                    except Exception:
                        logger.debug(
                            "Failed to send post-stop notification session=%s",
                            str(input_msg.session),
                            exc_info=True,
                        )
                    pass
                except Exception as e:
                    logger.exception(f"Failed to process message: {e}")
                    # 发送错误响应
                    try:
                        await self.output_adapter.send(
                            OutputMessage(
                                content=f"Error: {str(e)}",
                                message_type="error",
                            ),
                            str(input_msg.session),
                        )
                    except Exception as send_err:
                        logger.error(f"Failed to send error message: {send_err}")
        except asyncio.CancelledError:
            # 正常停止，不记录错误
            logger.info("Pipeline cancelled, shutting down...")
            raise
        except Exception as e:
            logger.exception(f"Pipeline error: {e}")
            raise
        finally:
            if self._dream_task is not None:
                self._dream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._dream_task
                self._dream_task = None
            await self.input_adapter.stop()
            await self.tool_manager.shutdown()

    async def _dream_scan_loop(self) -> None:
        """后台周期性扫描活跃 Context 并触发 DreamEngine。"""
        dream_engine = self.dream_engine
        dream_interval = self.dream_interval
        if dream_engine is None or dream_interval is None:
            return

        while self._running:
            try:
                await asyncio.sleep(dream_interval)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            # Duck typing: MemorySystemContextManager provides get_active_contexts + memory_system
            get_active = getattr(self.context_manager, "get_active_contexts", None)
            memory_system = getattr(self.context_manager, "memory_system", None)
            if get_active is None or memory_system is None:
                continue
            for ctx in self.context_manager.get_active_contexts():
                try:
                    count = await memory_system.get_unprocessed_history_count(ctx)
                except Exception as scan_err:
                    logger.debug("DreamEngine scan error for %s: %s", str(ctx.session_id), scan_err)
                    continue
                if count > 0:
                    scope_key = f"{str(ctx.session_id) if ctx.session_id else ''}:{ctx.user_id or ''}:{ctx.tenant_id or ''}"
                    lock = _dream_locks.setdefault(scope_key, asyncio.Lock())

                    logger.info(
                        "DreamEngine timer trigger, scope=%s, count=%d",
                        scope_key,
                        count,
                    )

                    async def _run_dream(
                        c: MemoryContext = ctx,
                        engine: DreamEngine = dream_engine,
                        lk: asyncio.Lock = lock,
                    ) -> None:
                        async with lk:
                            try:
                                await engine.run(c)
                            except Exception as dream_err:
                                logger.warning("DreamEngine failed: %s", dream_err)

                    asyncio.create_task(_run_dream())

    async def process_message(self, input_msg: InputMessage) -> AgentResult | None:
        """公共入口：处理单个消息"""
        return await self._process_message(input_msg)

    def is_session_active(self, session_id: str) -> bool:
        """Check if a turn is currently executing for this session."""
        task = self._session_tasks.get(session_id)
        return task is not None and not task.done()

    def has_active_sessions(self) -> bool:
        """Return True if any session has a running agent turn.

        Used by workspace cd/exit to check whether switching is safe.
        Subagent turns are covered — they run within their parent
        session's task and are tracked here.
        """
        return any(not task.done() for task in self._session_tasks.values())

    def get_active_turn_uuid(self, session_id: str) -> str | None:
        """Get turn UUID for the currently executing turn, or None."""
        if not self.is_session_active(session_id):
            return None
        return self._turn_uuids.get(session_id)

    async def _process_message(self, input_msg: InputMessage) -> AgentResult | None:
        """处理单个消息（内部入口）"""
        # 消息路由
        if self.router is not None:
            default_agent_name = (
                self.agent_descriptor.address.name
                if self.agent_descriptor is not None
                else self.agent.name
            )
            route_result = self.router.route(input_msg, default_agent_name=default_agent_name)
            session = route_result.session
        else:
            route_result = None
            session = input_msg.session
        session_id = session.session_id
        logger.info(f"Processing message: session_id={session_id}")

        prelock_dispatch_policy = None
        if self.command_processor is not None:
            prelock_parse_result = self.command_processor.parse(input_msg.content or "")
            if prelock_parse_result.invocation is not None:
                from modex_agent.commands.constants import CommandDispatchPolicy

                prelock_pending = await self._load_pending_approval_snapshot(session_id)
                prelock_dispatch_policy = self.command_processor.dispatch_policy(
                    prelock_parse_result.invocation,
                    CommandContext(
                        session_id=session_id,
                        input_msg=input_msg,
                        agent_name=self.agent.name,
                        skill_manager=self.skill_manager,
                        turn_store=self.turn_store,
                        pending_approval=prelock_pending,
                        runtime_info={"input_metadata": input_msg.metadata or {}},
                    ),
                )
                if prelock_dispatch_policy == CommandDispatchPolicy.BYPASS_QUEUE:
                    logger.info(
                        "Control command /%s received — handled by adapter-level interception",
                        prelock_parse_result.invocation.command,
                    )
                    return None
                if prelock_dispatch_policy == CommandDispatchPolicy.DROP_IF_BUSY:
                    logger.info("Drop-if-busy slash-command received; dropping")
                    return None

        # 去重检查
        if self.deduplicator is not None:
            message_id = input_msg.metadata.get("message_id") if input_msg.metadata else None
            if not message_id:
                import hashlib

                message_id = hashlib.sha256(
                    f"{session_id}:{input_msg.content}".encode()
                ).hexdigest()[:32]
            if self.deduplicator.is_duplicate(message_id):
                logger.info("Duplicate message skipped: %s", message_id)
                return None

        # 忙碌状态处理
        existing_task = self._session_tasks.get(session_id)
        if existing_task is not None and not existing_task.done():
            # Agent 正在执行中
            if self.busy_input_mode == BusyInputMode.INTERRUPT:
                existing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await existing_task
                # yield 事件循环给旧 task 的 finally 块一个执行机会
                await asyncio.sleep(0)
                # 任务已结束，fall through 到正常流程
            elif self.busy_input_mode == BusyInputMode.QUEUE:
                # Never queue slash commands as raw text — they would bypass the
                # command processor and lose their semantics when injected.
                if self.command_processor is not None:
                    prelock_parse = self.command_processor.parse(input_msg.content or "")
                    if prelock_parse.invocation is not None:
                        logger.info(
                            "Slash command %s dropped while agent is busy (session=%s)",
                            prelock_parse.invocation.command,
                            session_id,
                        )
                        await self.output_adapter.send(
                            OutputMessage(
                                content="Agent is currently processing. Please wait for the current turn to complete.",
                                session_id=session_id,
                                message_type="busy_notice",
                            ),
                            session_id,
                        )
                        return None
                queue = self._injection_queues.get(session_id)
                if queue:
                    await queue.put(input_msg.content or "")
                else:
                    logger.warning(
                        "No injection queue for session %s, dropping message", session_id
                    )
                return None
            elif self.busy_input_mode == BusyInputMode.STEER:
                if self.control_channel is not None:
                    from modex_agent.control.types import (
                        ControlCommand,
                        ControlCommandType,
                        ControlScope,
                    )

                    await self.control_channel.send(
                        ControlCommand(
                            command_id=str(uuid.uuid4()),
                            type=ControlCommandType.INJECT_STEER,
                            scope=ControlScope(session_id=session_id),
                            payload={"text": input_msg.content or ""},
                        )
                    )
                return None
            else:
                # Unknown mode, fall through (queue)
                pass

        # 获取或创建 session 级别的锁，防止同一 session 并发处理
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        lock_wait_start = time.monotonic()
        async with lock:
            lock_wait_ms = (time.monotonic() - lock_wait_start) * 1000
            if lock_wait_ms > 1000:  # warn if lock wait exceeds 1s
                logger.warning(
                    "Session lock wait: session=%s wait=%.0fms", session_id, lock_wait_ms
                )
            return await self._process_message_locked(input_msg, session_id, route_result, session=session)

    async def _preprocess_input(
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
        if self.sanitizer is not None:
            sanitized_content = self.sanitizer(sanitized_content)
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

    async def _assemble_context(
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
            agent_descriptor=self.agent_descriptor,
            tool_manager=self.tool_manager,
            skill_manager=self.skill_manager,
            context_builder=self.context_builder,
            append_user_message=append_user_message,
        )

    def _build_runtime_and_context(
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
        self._injection_queues.setdefault(session.session_id, asyncio.Queue(maxsize=50))

        # ---- typed TurnIdentity (new) ----
        from uuid import uuid4

        from modex_agent.runtime.models import TurnIdentity

        agent_id = (
            self.agent_descriptor.address.name
            if self.agent_descriptor is not None
            else self.agent.name
        )
        turn_identity = TurnIdentity(
            agent_id=agent_id,
            session=session,
            turn_id=uuid4().hex,
        )

        agent_context = AgentContext(
            system_prompt=context_state.system_prompt,
            history=context_state.history,
            tool_manager=self.tool_manager,
            session=session,
            comm_kind=self.agent_descriptor.comm_kind if self.agent_descriptor else None,
            max_iterations=self.max_iterations,
        )
        agent_context.system_prompt_pipeline = context_state.system_prompt_pipeline
        agent_context.identity = turn_identity
        # Per-turn snapshot (opaque PoolData) for hooks/agents that need
        # the resolved workspace's stores (e.g. experience dir). None when
        # no workspace manager is wired.
        agent_context.workspace_snapshot = pool_data

        # ---- governance (pending injection, etc.) — unconditional ----
        base_services = self.runtime_services
        base_gov = base_services.governance if base_services is not None else None
        governance = ctx_mgr.wrap_governance(base_gov or self.governance, session.session_id)

        # Resolve the turn-scoped turn/command stores. Precedence:
        # process-scope runtime_services override > per-turn pool snapshot
        # > pipeline-level self.turn_store / self.command_store.
        snapshot_turn_store = (
            pool_data.turn_store if pool_data is not None else self.turn_store
        )
        snapshot_command_store = (
            pool_data.command_store if pool_data is not None else self.command_store
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
                hooks=base_services.hooks if base_services is not None else self.hook_runner,
                interceptors=(
                    base_services.interceptors
                    if base_services is not None
                    else self.interceptor_chain
                ),
                approval=base_services.approval if base_services is not None else None,
                governance=governance,
                turn_store=(
                    base_services.turn_store
                    if base_services is not None and base_services.turn_store is not None
                    else snapshot_turn_store
                ),
                command_store=(
                    base_services.command_store
                    if base_services is not None and base_services.command_store is not None
                    else snapshot_command_store
                ),
                trace_store=snapshot_trace_store,
                pending_input_queue=self._injection_queues.get(session.session_id),
                safety=base_services.safety if base_services is not None else self.safety,
                runtime_context_manager=(
                    base_services.runtime_context_manager
                    if base_services is not None
                    and base_services.runtime_context_manager is not None
                    else self.runtime_context_manager
                ),
                control_channel=self.control_channel
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
                    control_channel=self.control_channel
                    or (base_services.control_channel if base_services is not None else None),
                ),
                state=ReActTurnState(
                    identity=turn_identity,
                    agent_kind=AgentKind.REACT,
                    phase=RTurnPhase.CREATED,
                ),
            )

        # Emitter selection
        if self.emitter_factory:
            emitter = self.emitter_factory(session.session_id)
        else:
            emitter = StreamingAwareEmitter(
                output_adapter=self.output_adapter,
                session_id=session.session_id,
                send_timeout=self.safety.turn.output_send_timeout_seconds,
            )

        return agent_context, emitter

    async def _execute_turn(
        self,
        agent_context: AgentContext,
        emitter: ContentEmitter,
        session_id: str,
        context_state: ContextState,
        input_metadata: dict[str, Any],
        ctx_mgr: ContextManager,
    ) -> AgentResult | None:
        """Execute a normal agent turn, including cleanup.

        Returns:
            AgentResult on successful turn, None if GraphInterrupt for approval.
        """
        agent_name = agent_context.session.agent_name
        result: AgentResult | None = None
        turn = self.safety.turn
        turn_start = time.monotonic()

        try:
            # Track this task for busy_input_mode handling
            turn_task = asyncio.current_task()
            if turn_task is not None:
                self._session_tasks[session_id] = turn_task

            # Generate turn UUID for control command scoping
            if agent_context.runtime is not None:
                turn_uuid = uuid.uuid4().hex
                agent_context.runtime.state.custom[TurnCustomKey.TURN_UUID] = turn_uuid
                self._turn_uuids[session_id] = turn_uuid

            try:
                result = await self.agent.run(agent_context, emitter)
            except GraphInterrupt as interrupt_exc:
                # ToolNode suspended for approval — snapshot persisted via TurnStateStore
                # Send approval prompts to user via UI
                if self._user_interface is not None:
                    requests = interrupt_exc.value
                    if isinstance(requests, list):
                        for req in requests:
                            await self._user_interface.render_message(
                                session_id,
                                format_approval_prompt(req),
                            )
                            break  # Only prompt the first one; user approves one at a time

                # Don't save user message — approval state takes over
                return None

            # 为最后一条 assistant 消息注入 attachments metadata
            if result and result.attachments:
                await inject_attachments_to_history(context_state.history, result.attachments)

            await ctx_mgr.save(
                session_id=session_id,
                user_message=None,
                assistant_result=result,
                metadata={"input_metadata": input_metadata},
            )
            elapsed = time.monotonic() - turn_start
            logger.info(
                "turn_done session=%s agent=%s stop_reason=%s elapsed=%.1fs",
                session_id,
                agent_name,
                result.stop_reason if result else "none",
                elapsed,
            )
            return result

        except asyncio.CancelledError:
            logger.warning(
                "Agent turn cancelled session=%s agent=%s",
                session_id,
                agent_name,
            )
            raise

        finally:
            # Clean up session task tracking
            self._session_tasks.pop(session_id, None)
            self._turn_uuids.pop(session_id, None)
            await _safe_flush(ctx_mgr, session_id, timeout=turn.memory_flush_timeout_seconds)
            # Turn 结束时的清理（带 timeout 保护）
            if self.on_session_end is not None:
                try:
                    await asyncio.wait_for(
                        self.on_session_end(session_id),
                        timeout=turn.hook_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    logger.warning("on_session_end cancelled for %s", session_id)
                except Exception:
                    logger.exception("on_session_end failed for %s", session_id)

    async def _handle_snapshot_approval(
        self,
        *,
        action: ApprovalAction | None,
        snapshot: TurnSnapshot,
        agent_context: AgentContext,
        emitter: ContentEmitter,
        session_id: str,
        context_state: ContextState,
        input_metadata: dict[str, Any],
        ctx_mgr: ContextManager,
        pool_data: PoolDataSnapshot | None = None,
    ) -> AgentResult | None:
        approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
        if approval is None:
            return None

        if action is not None:
            decision = (
                ApprovalDecision.ALLOWED
                if action == ApprovalAction.ALLOW
                else ApprovalDecision.DENIED
            )
            for req in approval.requests:
                current = approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
                if current == ApprovalDecision.PENDING:
                    approval.apply_decision(req.tool_call_id, decision)
                    break

        snapshot = ReActSnapshotPolicy.replace_approval(snapshot, approval)
        turn_store = pool_data.turn_store if pool_data is not None else self.turn_store
        if turn_store is None:
            logger.error("Approval resume requested but no TurnStateStore is configured")
            return None

        if not approval.every_tool_decided:
            await turn_store.save_turn(snapshot)
            if self._user_interface is not None:
                for req in approval.requests:
                    current = approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
                    if current == ApprovalDecision.PENDING:
                        await self._user_interface.render_message(
                            session_id,
                            format_approval_prompt(req),
                        )
                        break
            return None

        state = ReActSnapshotPolicy.state_from_snapshot(snapshot)
        if agent_context.runtime is None:
            return None
        agent_context.identity = snapshot.identity
        agent_context.runtime.state = state
        result = await self._execute_turn(
            agent_context,
            emitter,
            session_id,
            context_state,
            input_metadata,
            ctx_mgr,
        )
        if result is not None:
            await turn_store.delete_turn(snapshot.identity)
            await self._approval.drain(session_id)
        return result

    async def _load_pending_approval_snapshot(
        self, session_id: str, *, pool_data: PoolDataSnapshot | None = None,
    ) -> TurnSnapshot | None:
        turn_store = pool_data.turn_store if pool_data is not None else self.turn_store
        if turn_store is None:
            return None
        agent_id = self.agent.name
        snapshots = await turn_store.list_active_turns(
            StateQueryScope(
                agent_id=agent_id,
                session_id=session_id,
                phase=TurnPhase.SUSPENDED,
                reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
            )
        )
        if not snapshots:
            return None
        snapshots.sort(key=lambda snapshot: snapshot.created_at)
        return snapshots[-1]

    async def _build_turn_request(
        self,
        input_msg: InputMessage,
        session_id: str,
        input_metadata: dict[str, Any],
        pending_snapshot: TurnSnapshot | None,
        *,
        pool_data: PoolDataSnapshot | None = None,
    ) -> TurnRequest | None:
        if self.command_processor is None:
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

        parse_result = self.command_processor.parse(input_msg.content or "")
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
            agent_name=self.agent.name,
            skill_manager=self.skill_manager,
            turn_store=pool_data.turn_store if pool_data is not None else self.turn_store,
            pending_approval=pending_snapshot,
        )
        result = await self.command_processor.handle(input_msg.content or "", command_context)
        if result.notice:
            await self.output_adapter.send(
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

    async def _process_message_locked(
        self, input_msg: InputMessage, session_id: str, route_result: Any | None = None,
        *, session: SessionInfo,
    ) -> AgentResult | None:
        """Process one message while holding the session lock."""
        if self.on_session_start is not None:
            try:
                await asyncio.wait_for(
                    self.on_session_start(session_id),
                    timeout=self.safety.turn.hook_timeout_seconds,
                )
            except TimeoutError:
                logger.warning("on_session_start timeout for %s", session_id)
            except Exception:
                logger.exception("on_session_start failed for %s", session_id)
        ctx_mgr = (
            self.context_manager_factory(session_id)
            if self.context_manager_factory
            else self.context_manager
        )
        input_metadata = getattr(input_msg, "metadata", None) or {}

        # Resolve the per-turn PoolData snapshot once, at turn start, so a
        # workspace switch mid-turn cannot corrupt the in-flight turn.
        pool_data = self._resolve_pool_data(session_id)
        # Only the pool's main agent follows the workspace's pool_data
        # context_manager (to track workspace switches). A subagent registers
        # its OWN context_manager — its own system prompt + OUTPUT.md base dir
        # — and must never be overridden by the main agent's, otherwise every
        # subagent inherits the main prompt and loses its OUTPUT.md task.
        # (turn_store / command_store below are still shared — they are
        # pool-level and session-isolated, and the subagent needs them so its
        # runtime + FINALLY_TURN hooks are constructed.)
        if pool_data is not None and not self._is_subagent():
            ctx_mgr = pool_data.context_manager

        pending_snapshot = await self._load_pending_approval_snapshot(
            session_id, pool_data=pool_data,
        )
        turn_request = await self._build_turn_request(
            input_msg,
            session_id,
            input_metadata,
            pending_snapshot,
            pool_data=pool_data,
        )
        if turn_request is None:
            return None

        sanitized_content, media_blocks, media_processor = await self._preprocess_input(
            input_msg,
            session_id,
            input_metadata,
            route_result,
        )
        if sanitized_content is None:
            return None

        if turn_request.user_content is not None:
            sanitized_content = turn_request.user_content
            # Propagate content format from skill command to input_msg
            # so assemble_context picks it up for governance (XML truncation, etc.)
            cmd_result = turn_request.command_result
            if cmd_result is not None:
                if cmd_result.content_format is not None:
                    input_msg.content_format = cmd_result.content_format
                if cmd_result.truncatable_paths is not None:
                    input_msg.truncatable_paths = cmd_result.truncatable_paths
        elif not turn_request.append_user_message:
            sanitized_content = None

        approval_action = turn_request.approval_action
        is_approval_cmd, approval_state = await self._approval.detect(
            input_msg,
            session_id,
            input_metadata,
            pending_snapshot=pending_snapshot,
            approval_action=approval_action,
        )

        context_state = await self._assemble_context(
            session_id,
            input_msg,
            input_metadata,
            sanitized_content,
            media_blocks,
            media_processor,
            ctx_mgr,
            route_result,
            is_approval_cmd,
            append_user_message=turn_request.append_user_message,
        )
        agent_context, emitter = self._build_runtime_and_context(
            session,
            context_state,
            ctx_mgr,
            input_metadata=input_metadata,
            pool_data=pool_data,
        )

        if approval_state is not None:
            return await self._handle_snapshot_approval(
                action=approval_action,
                snapshot=approval_state,
                agent_context=agent_context,
                emitter=emitter,
                session_id=session_id,
                context_state=context_state,
                input_metadata=input_metadata,
                ctx_mgr=ctx_mgr,
                pool_data=pool_data,
            )

        if not turn_request.trigger_agent:
            return None

        return await self._execute_turn(
            agent_context,
            emitter,
            session_id,
            context_state,
            input_metadata,
            ctx_mgr,
        )

    async def cleanup_session_resources(self, session_id: str) -> None:
        """清理 per-session 资源（长时间运行避免内存泄漏）。

        应在 session 彻底结束时调用（用户断开、超时等），不应每个 turn 调用。
        """
        self._session_locks.pop(session_id, None)
        self._injection_queues.pop(session_id, None)
        self._session_tasks.pop(session_id, None)
        self._turn_uuids.pop(session_id, None)
        self._approval.cleanup_session(session_id)
        if self.control_channel is not None:
            try:
                await asyncio.wait_for(
                    self.control_channel.cleanup_session(session_id),
                    timeout=5.0,
                )
            except TimeoutError:
                logger.warning("cleanup_session timeout for %s", session_id)
            except Exception:
                logger.debug("cleanup_session failed for %s", session_id, exc_info=True)

    async def stop(self) -> None:
        """停止流水线"""
        self._running = False
        # 清理所有 lingering session 资源
        for sid in list(self._session_locks.keys()):
            await self.cleanup_session_resources(sid)
        logger.info("Pipeline stop requested, waiting for current message to complete...")
