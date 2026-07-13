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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.emitter import ContentEmitter
    from modex_agent.hook.abc import HookSpec
    from modex_agent.hook.runner import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.workspace import WorkspaceManager
    from modex_agent.runtime.store import TurnStateStore

from modex_agent.commands.models import (
    CommandContext,
    CommandProcessor,
)
from modex_agent.core.agent_runtime_config import BusyInputMode
from modex_agent.core.constants import ExecutionStrategy
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.skills import SkillManager

from modex_agent.approval.ui import ApprovalUserInterface
from modex_agent.control.exceptions import AgentControlError
from modex_agent.core.agent import Agent
from modex_agent.core.context import ContextManager
from modex_agent.core.emitter import AgentResult
from modex_agent.core.graph.interrupt import GraphInterrupt
from modex_agent.core.runtime_context import RuntimeContextManager
from modex_agent.core.tool_manager import ToolManager
from modex_agent.core.types import InputMessage
from modex_agent.memory.context_governance import ContextGovernance
from modex_agent.memory.consolidation import DreamEngine
from modex_agent.multi_agent import AgentDescriptor
from modex_agent.multi_agent.router import AgentMessageRouter
from modex_agent.pipeline.dream_scanner import DreamScanner
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.models import TurnSnapshot
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.utils.context_builder import MultiAgentContextBuilder
from modex_agent.utils.deduplicator import MessageDeduplicator
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter, OutputMessage
from modex_agent.pipeline.approval_renderer import ApprovalRenderer
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.pipeline.turn_runner import TurnRunner

logger = logging.getLogger(__name__)

_UNSET = object()


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
            runtime_services: process-scope services copied into each turn runtime
            workspace_manager: optional WorkspaceManager; when set together
                with ``pool_name`` each turn resolves its per-turn stores
                (context manager / turn store) from the
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
        self._emitter_factory = emitter_factory
        self.dream_engine = dream_engine
        self.dream_interval = dream_interval
        self.skill_manager = skill_manager
        self.hooks = list(hooks) if hooks else []

        self.router = router
        self.deduplicator = deduplicator
        self.agent_descriptor = agent_descriptor
        self.sanitizer = sanitizer
        self.context_manager_factory = context_manager_factory
        self.on_session_start = on_session_start
        self.on_session_end = on_session_end
        self.runtime_context_manager = runtime_context_manager
        self._governance = governance
        self.safety = safety or RuntimeSafetyPolicy()
        self.hook_runner = hook_runner
        self.interceptor_chain = interceptor_chain
        self.control_channel = control_channel
        self.busy_input_mode = busy_input_mode
        self.turn_store = turn_store
        self._runtime_services = runtime_services
        self.command_processor = command_processor
        # workspace_manager / pool_name are mutated post-construction by pool
        # wiring (e.g. bot pool_builder sets them after AgentPool creates the
        # pipeline). They back the properties below, which mirror mutations
        # into the TurnRunner so its captured copies don't go stale.
        self._workspace_manager = workspace_manager
        self._pool_name = pool_name
        self.pool_data_resolver = pool_data_resolver
        self._approval = ApprovalRenderer(
            agent=agent,
            user_interface=user_interface,
            on_drain=self._process_message,
        )
        self._approval_resumer = ApprovalResumer(
            agent=agent,
            turn_store=turn_store,
            user_interface=user_interface,
        )
        self._running = False
        self._dream_task: asyncio.Task | None = None
        self._dream_scanner: DreamScanner | None = None
        self._registry = TurnSessionRegistry()
        self._turn_context_builder = TurnContextBuilder(
            agent=agent,
            tool_manager=tool_manager,
            sanitizer=sanitizer,
            command_processor=command_processor,
            skill_manager=skill_manager,
            context_builder=context_builder,
            agent_descriptor=agent_descriptor,
            max_iterations=max_iterations,
            safety=self.safety,
            runtime_services=runtime_services,
            runtime_context_manager=runtime_context_manager,
            governance=governance,
            hook_runner=hook_runner,
            interceptor_chain=interceptor_chain,
            control_channel=control_channel,
            emitter_factory=emitter_factory,
            output_adapter=output_adapter,
            turn_store=turn_store,
            registry=self._registry,
        )
        is_external = (
            agent_descriptor is not None
            and agent_descriptor.execution_strategy == ExecutionStrategy.EXTERNAL_CODING
        )

        if is_external:
            # Lazy import: ReAct pools must never load the external_coding package.
            from modex_agent.agents.external_coding.turn_runner import ExternalTurnRunner

            self._turn_runner = ExternalTurnRunner(
                agent=agent,
                emitter_factory=emitter_factory,
                output_adapter=output_adapter,
                registry=self._registry,
                on_session_start=on_session_start,
                on_session_end=on_session_end,
                safety=self.safety,
            )
        else:
            self._turn_runner = TurnRunner(
                agent=agent,
                context_manager=context_manager,
                context_manager_factory=context_manager_factory,
                on_session_start=on_session_start,
                on_session_end=on_session_end,
                safety=self.safety,
                turn_store=turn_store,
                registry=self._registry,
                builder=self._turn_context_builder,
                resumer=self._approval_resumer,
                approval=self._approval,
                workspace_manager=workspace_manager,
                pool_name=pool_name,
                pool_data_resolver=pool_data_resolver,
                agent_descriptor=agent_descriptor,
            )

    @property
    def _user_interface(self):  # delegates to renderer so pool injection reaches handle()
        return self._approval._user_interface

    @_user_interface.setter
    def _user_interface(self, value):
        self._approval._user_interface = value

    @property
    def workspace_manager(self) -> WorkspaceManager | None:
        return self._workspace_manager

    @workspace_manager.setter
    def workspace_manager(self, value: WorkspaceManager | None) -> None:
        self._workspace_manager = value
        # Mirror into TurnRunner so its captured copy stays current when pool
        # wiring mutates this attribute after pipeline construction.
        self._turn_runner._workspace_manager = value

    @property
    def pool_name(self) -> str | None:
        return self._pool_name

    @pool_name.setter
    def pool_name(self, value: str | None) -> None:
        self._pool_name = value
        self._turn_runner._pool_name = value

    @property
    def runtime_services(self) -> AgentRuntimeServices | None:
        return self._runtime_services

    @runtime_services.setter
    def runtime_services(self, value: AgentRuntimeServices | None) -> None:
        self._runtime_services = value
        # Mirror into TurnContextBuilder so post-construction wiring (e.g.
        # main-pool approval wiring in pool_builder) reaches the per-turn
        # runtime via base_services.approval. Mirrors into the builder like
        # emitter_factory; cf. workspace_manager / pool_name which mirror
        # into the TurnRunner instead.
        self._turn_context_builder._runtime_services = value

    @property
    def governance(self) -> ContextGovernance | None:
        return self._governance

    @governance.setter
    def governance(self, value: ContextGovernance | None) -> None:
        self._governance = value
        # Mirror into TurnContextBuilder so post-construction wiring (e.g.
        # main-pool governance = create_governance(...) in pool_builder) reaches
        # the per-turn runtime via base_gov or self._governance. Without this,
        # sanitizer never runs on the main agent and dangling tool_calls reach
        # the provider (400). Same mirror pattern as runtime_services.
        self._turn_context_builder._governance = value

    @property
    def emitter_factory(self) -> Callable[..., ContentEmitter] | None:
        return self._emitter_factory

    @emitter_factory.setter
    def emitter_factory(self, value: Callable[..., ContentEmitter] | None) -> None:
        self._emitter_factory = value
        self._turn_context_builder._emitter_factory = value
        if hasattr(self._turn_runner, "_emitter_factory"):
            self._turn_runner._emitter_factory = value

    async def run(self) -> None:
        """运行流水线"""
        self._running = True
        await self.input_adapter.start()

        if (
            self.dream_engine is not None
            and self.dream_interval is not None
            and self.dream_interval > 0
        ):
            self._dream_scanner = DreamScanner(
                dream_engine=self.dream_engine,
                dream_interval=self.dream_interval,
                context_manager=self.context_manager,
            )
            self._dream_task = asyncio.create_task(self._dream_scanner.run_forever())

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
                if self._dream_scanner is not None:
                    self._dream_scanner.stop()
                self._dream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._dream_task
                self._dream_task = None
                self._dream_scanner = None
            await self.input_adapter.stop()

    async def process_message(self, input_msg: InputMessage) -> AgentResult | None:
        """公共入口：处理单个消息"""
        return await self._process_message(input_msg)

    def is_session_active(self, session_id: str) -> bool:
        """Check if a turn is currently executing for this session."""
        return self._registry.is_active(session_id)

    def has_active_sessions(self) -> bool:
        """Return True if any session has a running agent turn.

        Used by workspace cd/exit to check whether switching is safe.
        Subagent turns are covered — they run within their parent
        session's task and are tracked here.
        """
        return self._registry.has_active()

    def get_active_turn_uuid(self, session_id: str) -> str | None:
        """Get turn UUID for the currently executing turn, or None."""
        return self._registry.get_turn_uuid(session_id)

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

        # 去重检查 — structured approval decisions carry no message_id and would
        # otherwise hash-collide; bypass dedup for them.
        if self.deduplicator is not None and input_msg.approval_decision is None:
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
        existing_task = self._registry.get_session_task(session_id)
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
                # Reuse the pre-lock parse (prelock_parse_result) — command_processor
                # is stateless and input_msg.content is unchanged.
                if self.command_processor is not None:
                    if prelock_parse_result.invocation is not None:
                        logger.info(
                            "Slash command %s dropped while agent is busy (session=%s)",
                            prelock_parse_result.invocation.command,
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
                queue = self._registry.get_queue(session_id)
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
        lock = self._registry.set_session_lock(session_id)
        lock_wait_start = time.monotonic()
        async with lock:
            lock_wait_ms = (time.monotonic() - lock_wait_start) * 1000
            if lock_wait_ms > 1000:  # warn if lock wait exceeds 1s
                logger.warning(
                    "Session lock wait: session=%s wait=%.0fms", session_id, lock_wait_ms
                )
            return await self._turn_runner.process_locked(input_msg, session_id, route_result, session=session)

    async def _load_pending_approval_snapshot(
        self, session_id: str, *, pool_data: PoolDataSnapshot | None = None,
    ) -> TurnSnapshot | None:
        return await self._approval_resumer.load_pending(session_id, pool_data=pool_data)

    async def cleanup_session_resources(self, session_id: str) -> None:
        """清理 per-session 资源（长时间运行避免内存泄漏）。

        应在 session 彻底结束时调用（用户断开、超时等），不应每个 turn 调用。
        """
        self._registry.cleanup(session_id)
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
        for sid in self._registry.session_ids():
            await self.cleanup_session_resources(sid)
        logger.info("Pipeline stop requested, waiting for current message to complete...")
