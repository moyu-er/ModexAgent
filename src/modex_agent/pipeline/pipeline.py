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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.commands.skill import SkillResolver
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.context import ContextManager
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.hook.runner import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.multi_agent import AgentDescriptor
    from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
    from modex_agent.runtime.context import RuntimeContextManager
    from modex_agent.runtime.store import TurnStateStore

from modex_agent.adapters.output import OutputAdapter
from modex_agent.commands.models import (
    CommandContext,
    CommandProcessor,
)
from modex_agent.control.exceptions import AgentControlError
from modex_agent.core.agent import Agent
from modex_agent.core.emitter import AgentResult
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.types import InputMessage, OutputMessage, OutputMessageType
from modex_agent.memory.consolidation import DreamEngine
from modex_agent.multi_agent.router import AgentMessageRouter
from modex_agent.pipeline.adapters import InputAdapter
from modex_agent.pipeline.busy_input import BusyInputMode
from modex_agent.pipeline.dream_scanner import DreamScanner
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.pipeline.turn_runner_abc import TurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.models import TurnSnapshot
from modex_agent.utils.deduplicator import MessageDeduplicator
from modex_graph.exceptions import GraphInterrupt

logger = logging.getLogger(__name__)


class AgentPipeline:
    """Agent 流水线 - 编排完整的端到端流程

    Slimmed facade (ticket 5b): the pipeline owns lifecycle (``run``/``stop``),
    pre-lock dispatch (``_process_message``: route/dedup/busy-mode/lock),
    session-query delegation, dream-task management, and the
    ``control_channel``. All turn-execution collaborators live inside the
    ``TurnRunner`` (constructed by the factory, not the pipeline).

    Backward-compat: read-only delegation properties (``hook_runner``,
    ``hooks``, ``skill_resolver``, ``context_manager``, ``tool_manager``,
    ``sanitizer``, ``agent_descriptor``, ``turn_store``, ``interceptor_chain``,
    ``runtime_context_manager``, ``_turn_context_builder``) expose the
    turn_runner's internals for code that reads them. The 5 mirror SETTER
    properties (``workspace_manager``, ``pool_name``, ``runtime_services``,
    ``governance``, ``emitter_factory``) are deleted — post-construction
    wiring targets the runner's sub-objects directly.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        turn_runner: TurnRunner,
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        registry: TurnSessionRegistry,
        safety: RuntimeSafetyPolicy | None = None,
        router: AgentMessageRouter | None = None,
        command_processor: CommandProcessor | None = None,
        deduplicator: MessageDeduplicator | None = None,
        busy_input_mode: BusyInputMode = BusyInputMode.QUEUE,
        control_channel: InMemoryControlChannel | None = None,
        dream_engine: DreamEngine | None = None,
        dream_interval: float | None = None,
    ) -> None:
        self.agent = agent
        self._turn_runner = turn_runner
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter
        self._registry = registry
        self.safety = safety or RuntimeSafetyPolicy()
        self.router = router
        self.command_processor = command_processor
        self.deduplicator = deduplicator
        self.busy_input_mode = busy_input_mode
        self.control_channel = control_channel
        self.dream_engine = dream_engine
        self.dream_interval = dream_interval
        self._running = False
        self._dream_task: asyncio.Task | None = None
        self._dream_scanner: DreamScanner | None = None

    # ── Backward-compat read-only delegation properties ──────────────────

    @property
    def hook_runner(self) -> HookRunner | None:
        return self._turn_runner.hook_runner

    @property
    def hooks(self) -> list[Any]:
        return self._turn_runner.hooks

    @property
    def skill_resolver(self) -> SkillResolver | None:
        return self._turn_runner.skill_resolver

    @property
    def context_manager(self) -> ContextManager | None:
        return self._turn_runner.context_manager

    @property
    def tool_manager(self) -> ToolManager | None:
        return self._turn_runner.tool_manager

    @property
    def sanitizer(self) -> Callable[[str], str] | None:
        return self._turn_runner.sanitizer

    @property
    def agent_descriptor(self) -> AgentDescriptor | None:
        return self._turn_runner.agent_descriptor

    @property
    def turn_store(self) -> TurnStateStore | None:
        return self._turn_runner.turn_store

    @property
    def interceptor_chain(self) -> InterceptorChain | None:
        return self._turn_runner.interceptor_chain

    @property
    def runtime_context_manager(self) -> RuntimeContextManager | None:
        return self._turn_runner.runtime_context_manager

    @property
    def _turn_context_builder(self) -> TurnContextBuilder | None:
        return self._turn_runner.turn_context_builder

    async def run(self) -> None:
        """运行流水线"""
        self._running = True
        await self.input_adapter.start()

        if (
            self.dream_engine is not None
            and self.dream_interval is not None
            and self.dream_interval > 0
            and self.context_manager is not None
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
                    raise
                except AgentControlError:
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
                    try:
                        await self.output_adapter.send(
                            OutputMessage(
                                content=f"Error: {str(e)}",
                                message_type=OutputMessageType.ERROR,
                            ),
                            str(input_msg.session),
                        )
                    except Exception as send_err:
                        logger.error(f"Failed to send error message: {send_err}")
        except asyncio.CancelledError:
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
        """Return True if any session has a running agent turn."""
        return self._registry.has_active()

    def get_active_turn_uuid(self, session_id: str) -> str | None:
        """Get turn UUID for the currently executing turn, or None."""
        return self._registry.get_turn_uuid(session_id)

    def cancel_active_turn(self, session_id: str) -> bool:
        """Wake and cancel the currently executing turn for this session."""
        return self._registry.cancel_turn(session_id)

    async def _process_message(self, input_msg: InputMessage) -> AgentResult | None:
        """处理单个消息（内部入口）"""
        if self.router is not None:
            route_result = self.router.route(input_msg)
            session = route_result.session
        else:
            route_result = None
            session = input_msg.session
        session_id = session.session_id
        logger.info(f"Processing message: session_id={session_id}")

        prelock_parse_result = None
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
                        skill_resolver=self._turn_runner.skill_resolver,
                        turn_store=self._turn_runner.turn_store,
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

        existing_task = self._registry.get_session_task(session_id)
        if existing_task is not None and not existing_task.done():
            if self.busy_input_mode == BusyInputMode.INTERRUPT:
                existing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await existing_task
                await asyncio.sleep(0)
            elif self.busy_input_mode == BusyInputMode.QUEUE:
                if (
                    self.command_processor is not None
                    and prelock_parse_result is not None
                    and prelock_parse_result.invocation is not None
                ):
                        logger.info(
                            "Slash command %s dropped while agent is busy (session=%s)",
                            prelock_parse_result.invocation.command,
                            session_id,
                        )
                        await self.output_adapter.send(
                            OutputMessage(
                                content="Agent is currently processing. Please wait for the current turn to complete.",
                                session_id=session_id,
                                message_type=OutputMessageType.BUSY_NOTICE,
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
                pass

        lock = self._registry.set_session_lock(session_id)
        lock_wait_start = time.monotonic()
        async with lock:
            lock_wait_ms = (time.monotonic() - lock_wait_start) * 1000
            if lock_wait_ms > 1000:
                logger.warning(
                    "Session lock wait: session=%s wait=%.0fms", session_id, lock_wait_ms
                )
            return await self._turn_runner.process_locked(input_msg, session_id, route_result, session=session)

    async def _load_pending_approval_snapshot(
        self, session_id: str, *, pool_data: PoolDataSnapshot | None = None,
    ) -> TurnSnapshot | None:
        return await self._turn_runner.load_pending_approval(session_id, pool_data=pool_data)

    async def cleanup_session_resources(self, session_id: str) -> None:
        """清理 per-session 资源（长时间运行避免内存泄漏）。

        应在 session 彻底结束时调用（用户断开、超时等），不应每个 turn 调用。
        """
        self._registry.cleanup(session_id)
        await self._turn_runner.cleanup_session(session_id)
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
        for sid in self._registry.session_ids():
            await self.cleanup_session_resources(sid)
        logger.info("Pipeline stop requested, waiting for current message to complete...")
        try:
            hook_runner = self.hook_runner
            if hook_runner is not None:
                await hook_runner.aclose()
        finally:
            await self.agent.stop()
