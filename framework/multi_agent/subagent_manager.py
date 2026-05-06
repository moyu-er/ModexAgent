from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from framework.core.context import InMemoryContextManager
from framework.core.emitter import AgentResult, BufferingEmitter

from .address import AgentAddress
from .coordinator import InMemoryTaskCoordinator, NullTaskCoordinator, TaskCoordinator, TaskRecord
from .descriptor import AgentDescriptor
from .factory import AgentFactory
from framework.control.task_supervision import TaskSupervisor, TimeoutSupervisionPolicy

if TYPE_CHECKING:
    from framework.messaging.broker import MessageBroker

logger = logging.getLogger(__name__)


current_conversation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_conversation_id", default=None
)


@dataclass
class TaskCoordinationConfig:
    """任务协调配置。"""

    enable_for_subagent: bool = True
    default_timeout_seconds: float = 180.0
    supervision_check_interval: float = 5.0
    supervision_emit_heartbeat: bool = True


@dataclass
class _SubagentRequest:
    """内部请求对象，封装一次 spawn_subagent_sync 调用所需的所有信息。"""

    exec_id: str
    parent_address: AgentAddress
    descriptor: AgentDescriptor
    task_prompt: str
    conversation_id: str
    tool_manager: Any | None
    skill_manager: Any | None
    future: asyncio.Future[AgentResult]


class SubagentManager:
    """子 Agent 管理器（仅同步模式）。

    通过内部 ``asyncio.Queue`` 实现单线程串行消费：
    - ``spawn_and_wait()`` 将请求打包入队，通过 ``asyncio.Future`` 等待结果
    - 独立消费者协程 ``_consume_requests()`` 从队列中串行取出并执行
    - 执行完成后通过 ``future.set_result()`` / ``set_exception()`` 返回

    所有结果直接返回给调用者，不通过 inbox/broker 异步投递。
    与其他 Agent（AgentPool 常驻 Agent）一样采用"队列消费"模式，保持架构统一。
    """

    def __init__(
        self,
        broker: MessageBroker,
        agent_factory: AgentFactory,
        task_coordinator: TaskCoordinator | None = None,
        coordination_config: TaskCoordinationConfig | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        on_task_complete: Any | None = None,
    ):
        self._broker = broker
        self._agent_factory = agent_factory
        self._config = coordination_config or TaskCoordinationConfig()
        if task_coordinator is not None:
            self._coordinator = task_coordinator
        elif not self._config.enable_for_subagent:
            self._coordinator = NullTaskCoordinator()
        else:
            self._coordinator = InMemoryTaskCoordinator()
        self._supervisor = TaskSupervisor(
            self._coordinator,
            check_interval=self._config.supervision_check_interval,
            emit_heartbeat=self._config.supervision_emit_heartbeat,
        )
        self._sanitizer = sanitizer
        self._command_interceptor = command_interceptor
        self._on_task_complete = on_task_complete
        self._persistent_contexts: dict[str, Any] = {}

        # 内部队列与消费机制
        self._request_queue: asyncio.Queue[_SubagentRequest] = asyncio.Queue()
        self._consumer_task: asyncio.Task | None = None
        self._pending_futures: dict[str, asyncio.Future[AgentResult]] = {}

    async def start(self) -> None:
        """启动请求消费协程。"""
        await self._broker.start()
        if self._consumer_task is None or self._consumer_task.done():
            self._consumer_task = asyncio.create_task(self._consume_requests())

    async def stop(self) -> None:
        """停止消费协程并清理资源。"""
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None
        # 取消所有还在等待的 future
        for fut in list(self._pending_futures.values()):
            if not fut.done():
                fut.cancel()
        self._pending_futures.clear()

    async def _consume_requests(self) -> None:
        """串行消费 subagent 请求。

        从队列中逐个取出请求，调用 ``_execute_subagent()`` 执行，
        执行完成后通过 ``future`` 返回结果。确保同一时刻只有一个 subagent 在运行。
        """
        while True:
            try:
                request = await self._request_queue.get()
            except asyncio.CancelledError:
                break

            try:
                result = await self._execute_subagent(request)
            except asyncio.CancelledError:
                result = AgentResult(content="", stop_reason="cancelled")
            except Exception as e:
                logger.exception("Subagent execution failed for exec_id %s", request.exec_id)
                result = AgentResult(
                    content=f"Error: {e}", stop_reason="error", error=str(e)
                )

            # 从 pending 中移除并设置结果
            self._pending_futures.pop(request.exec_id, None)
            if not request.future.done():
                request.future.set_result(result)

            self._request_queue.task_done()

    async def spawn_and_wait(
        self,
        parent_address: AgentAddress,
        descriptor: AgentDescriptor,
        task_prompt: str,
        conversation_id: str,
        timeout: float = 60.0,
        tool_manager: Any | None = None,
        skill_manager: Any | None = None,
    ) -> AgentResult:
        """同步模式：将请求入队，等待消费者串行执行并返回结果。

        Args:
            parent_address: 调用方 Agent 地址
            descriptor: 子 Agent 描述符
            task_prompt: 任务提示词
            conversation_id: 对话 ID
            timeout: 超时时间（秒）
            tool_manager: 可选的独立 ToolManager
            skill_manager: 可选的独立 SkillManager

        Returns:
            AgentResult: 子 Agent 执行结果
        """
        if self._consumer_task is None or self._consumer_task.done():
            await self.start()

        # 会话 ID 格式：{conversation_id}:{caller}:{subagent}:{uuid}
        exec_id = (
            f"{conversation_id}:{parent_address.name}:{descriptor.address.name}:"
            f"{uuid.uuid4().hex[:8]}"
        )

        future: asyncio.Future[AgentResult] = asyncio.get_running_loop().create_future()
        self._pending_futures[exec_id] = future

        request = _SubagentRequest(
            exec_id=exec_id,
            parent_address=parent_address,
            descriptor=descriptor,
            task_prompt=task_prompt,
            conversation_id=conversation_id,
            tool_manager=tool_manager,
            skill_manager=skill_manager,
            future=future,
        )
        await self._request_queue.put(request)

        # 通过 TaskCoordinator + Supervisor 包装超时控制
        if isinstance(self._coordinator, NullTaskCoordinator):
            try:
                return await asyncio.wait_for(
                    future, timeout=timeout or self._config.default_timeout_seconds
                )
            except TimeoutError:
                if not future.done():
                    future.cancel()
                return AgentResult(
                    content="Subagent execution timed out",
                    stop_reason="timeout",
                )
            finally:
                self._pending_futures.pop(exec_id, None)

        task_id = f"subagent:{descriptor.address.name}:{exec_id.split(':')[-1]}"
        await self._coordinator.register_task(
            task_id,
            TaskRecord(
                task_id=task_id,
                task_type="subagent_spawn_and_wait",
                created_at=time.time(),
                conversation_id=conversation_id,
                source_agent=parent_address.name,
                target_agent=descriptor.address.name,
            ),
        )
        await self._coordinator.bind_policy(
            task_id,
            TimeoutSupervisionPolicy.from_duration(
                timeout or self._config.default_timeout_seconds
            ),
        )
        try:
            return await self._supervisor.supervise(
                task_id,
                asyncio.wait_for(
                    future,
                    timeout=timeout or self._config.default_timeout_seconds,
                ),
            )
        except TimeoutError:
            if not future.done():
                future.cancel()
            return AgentResult(
                content="Subagent execution timed out",
                stop_reason="timeout",
            )
        finally:
            self._pending_futures.pop(exec_id, None)

    @staticmethod
    async def _cleanup_subagent_session(
        session_id: str,
        context_strategy: str,
        context_manager: Any | None,
        on_task_complete: Any | None,
    ) -> None:
        """清理 subagent 会话：清除 ephemeral 上下文，调用业务层完成回调。"""
        if context_strategy == "ephemeral" and context_manager is not None:
            try:
                await context_manager.clear(session_id)
            except Exception:
                logger.exception("Failed to clear ephemeral context for %s", session_id)
        if on_task_complete is not None:
            try:
                await on_task_complete(session_id)
            except Exception:
                logger.exception("on_task_complete failed for %s", session_id)

    async def _execute_subagent(self, request: _SubagentRequest) -> AgentResult:
        """执行子 Agent。

        使用 ``request.exec_id`` 作为 ``session_id``，确保每次调用都有唯一标识。
        记忆清理通过注入的 SubagentMemoryCleanupHook 完成，由 Agent.run 的
        finally 块触发，确保无论成功或失败都会执行。
        """
        session_id = request.exec_id
        descriptor = request.descriptor

        if descriptor.context_manager is not None:
            context_manager = descriptor.context_manager
        elif descriptor.context_strategy == "persistent":
            context_manager = self._persistent_contexts.get(session_id)
            if context_manager is None:
                context_manager = InMemoryContextManager(
                    base_system_prompt=descriptor.system_prompt_template or ""
                )
                self._persistent_contexts[session_id] = context_manager
        else:
            from framework.core.context import EphemeralContextManager

            context_manager = EphemeralContextManager(
                base_system_prompt=descriptor.system_prompt_template or ""
            )

        from framework.hook.builtin import SubagentMemoryCleanupHook

        hooks: list[Any] = [
            SubagentMemoryCleanupHook(
                cleanup_fn=lambda sid: self._cleanup_subagent_session(
                    sid,
                    descriptor.context_strategy,
                    context_manager,
                    self._on_task_complete,
                ),
                session_id=session_id,
            ),
        ]

        try:
            instance = await self._agent_factory.create_agent(
                descriptor,
                mode="session",
                conversation_id=request.conversation_id,
                context_manager=context_manager,
                tool_manager=request.tool_manager,
                skill_manager=request.skill_manager,
                sanitizer=self._sanitizer,
                command_interceptor=self._command_interceptor,
                subagent_manager=self,
                hooks=hooks,
            )

            from framework.core.events import EmitterConfig
            from framework.core.types import InputMessage

            assert instance.session is not None, "session agent must have a session"
            # 同步 subagent 必须使用真正的非流式输出：
            # - BufferingEmitter.wants_streaming() 返回 False
            # - ReActAgent._request_llm 走 provider.chat() 路径（非 chat_stream）
            # - 模型本身一次性返回完整内容，不是伪流式
            emitter = BufferingEmitter(config=EmitterConfig())
            result = await instance.session.process_message(
                message=InputMessage(content=request.task_prompt),
                emitter=emitter,
                session_id=session_id,
            )
            # 防御性：如果 result.content 为空但 emitter 收集了内容，使用 emitter 内容
            if not result.content and emitter.get_content():
                result.content = emitter.get_content()
            if not result.reasoning and emitter.get_reasoning():
                result.reasoning = emitter.get_reasoning()
        except Exception as e:
            logger.exception("Subagent execution failed: %s", session_id)
            result = AgentResult(
                content=f"Subagent execution failed: {e}",
                stop_reason="error",
                error=str(e),
            )

        return result
