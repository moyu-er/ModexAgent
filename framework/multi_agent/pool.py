from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from framework.core.context import ContextManager
from framework.core.emitter import AgentResult
from framework.core.graph.interrupt import GraphInterrupt
from framework.core.llm_struct import RuntimeSafetyPolicy
from framework.core.tool_manager import InMemoryToolManager
from framework.core.types import InputMessage
from framework.messaging.broker import BrokerMessage, MessageBroker

from .address import AgentAddress
from .bus import AgentMessageBus
from .descriptor import AgentDescriptor, AgentInstance
from .envelope import AgentMessageEnvelope
from .factory import AgentFactory
from .inbox.consumer import InboxConsumer
from .inbox.producer import InboxProducer
from .inbox.types import InboxMessage
from .registry import AgentProfile, AgentRegistry
from .session_id import DefaultSessionIdStrategy, SessionIdStrategy
from .state import AgentState

logger = logging.getLogger(__name__)

DEFAULT_INBOX_POLL_INTERVAL: float = 10.0
MAX_ENVELOPE_HOPS: int = 5


@dataclass
class SessionMeta:
    """Per-session metadata for lifecycle tracking."""
    agent_name: str
    created_at: float
    last_active: float
    is_dynamic: bool = False


@dataclass
class SessionRetentionPolicy:
    """Controls session cleanup for subagent task sessions."""
    max_sessions_per_subagent: int = 10
    max_sessions_global: int = 200
    ttl_seconds: float = 86400.0
    cleanup_interval_seconds: float = 1800.0


class AgentPool(AgentRegistry):
    """Agent 生命周期管理池。"""

    def __init__(
        self,
        broker: MessageBroker,
        agent_factory: AgentFactory,
        default_context_manager: ContextManager | None = None,
        agent_bus: AgentMessageBus | None = None,
        inbox_consumer: InboxConsumer | None = None,
        *,
        enable_inbox_polling: bool = True,
        inbox_poll_interval: float = 10.0,
        default_context_manager_factory: Callable[[str], ContextManager] | None = None,
        session_strategy: SessionIdStrategy | None = None,
        safety: RuntimeSafetyPolicy | None = None,
        retention: SessionRetentionPolicy | None = None,
    ):
        self._agents: dict[str, AgentInstance] = {}
        self._status: dict[str, AgentState] = {}
        self._broker = broker
        self._agent_factory = agent_factory
        self._default_context_manager = default_context_manager
        self._default_context_manager_factory = default_context_manager_factory
        self._agent_bus = agent_bus
        self._inbox_consumer = inbox_consumer
        self._enable_inbox_polling = enable_inbox_polling
        self._inbox_poll_interval = inbox_poll_interval
        self._session_strategy = session_strategy or DefaultSessionIdStrategy()
        self._safety = safety or RuntimeSafetyPolicy()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_meta: dict[str, SessionMeta] = {}
        self._sync_futures: dict[str, asyncio.Future] = {}
        self._retention = retention or SessionRetentionPolicy()
        self._cleanup_task: asyncio.Task | None = None
        self._consumers: dict[str, asyncio.Task] = {}
        self._agent_tasks: dict[str, list[asyncio.Task]] = {}
        self._active_session_counts: dict[str, int] = {}
        self._error_counts: dict[str, int] = {}
        self._max_error_retries: int = 5
        self._max_backoff_seconds: float = 10.0
        self._dispatch_locks: dict[str, asyncio.Lock] = {}
        self._inbox_poll_task: asyncio.Task | None = None
        self._valid_transitions: dict[AgentState, set[AgentState]] = {
            AgentState.INITIALIZING: {AgentState.IDLE, AgentState.ERROR, AgentState.SHUTTING_DOWN},
            AgentState.IDLE: {AgentState.WORKING, AgentState.ERROR, AgentState.SHUTTING_DOWN},
            AgentState.WORKING: {AgentState.IDLE, AgentState.ERROR, AgentState.SHUTTING_DOWN},
            AgentState.ERROR: {AgentState.IDLE, AgentState.SHUTTING_DOWN},
            AgentState.SHUTTING_DOWN: {AgentState.SHUTDOWN},
            AgentState.SHUTDOWN: set(),
        }
        if self._enable_inbox_polling:
            self._inbox_poll_task = asyncio.create_task(self._poll_inbox_for_idle_agents())
        self._cleanup_task = asyncio.create_task(self._cleanup_stale_sessions())

    def _transition(self, name: str, new_state: AgentState, reason: str = "") -> None:
        current = self._status.get(name, AgentState.SHUTDOWN)
        valid = self._valid_transitions.get(current, set())
        if new_state not in valid:
            logger.warning(
                "Invalid state transition: %s -> %s for %s (reason=%s)",
                current.value,
                new_state.value,
                name,
                reason or "unspecified",
            )
        if current != new_state:
            logger.info(
                "Agent state transition: %s %s -> %s reason=%s",
                name,
                current.value,
                new_state.value,
                reason or "unspecified",
            )
        self._status[name] = new_state

    async def register_resident(
        self,
        descriptor: AgentDescriptor,
        *,
        context_manager: ContextManager | None = None,
        tool_manager: InMemoryToolManager | None = None,
        skill_manager: Any | None = None,
        output_adapter: Any | None = None,
        context_manager_factory: Callable[[str], ContextManager] | None = None,
    ) -> AgentInstance:
        """注册常驻 Agent。

        Args:
            descriptor: Agent 描述符（身份、能力、策略配置）
            context_manager: 独立的上下文管理器（如 MemorySystemContextManager），
                不传则使用 pool 默认值或 descriptor 中的配置
            tool_manager: 独立的工具管理器，不传则使用 AgentFactory 默认值
            skill_manager: 独立的技能管理器，不传则使用 AgentFactory 默认值
            output_adapter: 可选的自定义输出适配器（如 NullOutputAdapter）
            context_manager_factory: 可选的 ContextManager 工厂函数，接收 session_id 返回 ContextManager
        """
        name = descriptor.address.name
        self._transition(name, AgentState.INITIALIZING, reason="register_resident")
        ctx_mgr = context_manager or self._default_context_manager
        if ctx_mgr is None and descriptor.context_manager is not None:
            ctx_mgr = descriptor.context_manager
        ctx_mgr_factory = context_manager_factory or self._default_context_manager_factory
        instance = await self._agent_factory.create_agent(
            descriptor,
            mode="pipeline",
            context_manager=ctx_mgr,
            broker=self._broker,
            tool_manager=tool_manager,
            skill_manager=skill_manager,
            output_adapter=output_adapter,
            context_manager_factory=ctx_mgr_factory,
        )
        self._agents[name] = instance
        self._transition(name, AgentState.IDLE, reason="register_resident_complete")
        consumer_task = asyncio.create_task(self._consume_messages(instance, descriptor))
        consumer_task.add_done_callback(lambda t, n=name: self._on_consumer_done(t, n))
        self._consumers[name] = consumer_task
        return instance

    def _track_agent_task(self, agent_name: str, task: asyncio.Task) -> None:
        """追踪 agent 的后台处理任务。"""
        tasks = self._agent_tasks.setdefault(agent_name, [])
        tasks.append(task)
        task.add_done_callback(lambda t: self._prune_agent_task(agent_name, t))

    def _prune_agent_task(self, agent_name: str, task: asyncio.Task) -> None:
        """清理已完成的任务引用。"""
        tasks = self._agent_tasks.get(agent_name, [])
        if task in tasks:
            tasks.remove(task)

    def _on_consumer_done(self, task: asyncio.Task[Any], agent_name: str) -> None:
        """Consumer task 完成回调：记录异常并尝试恢复。"""
        if task.cancelled():
            logger.info("Consumer task for %s was cancelled", agent_name)
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Consumer task for %s exited with error",
                agent_name,
                exc_info=exc,
            )
        else:
            logger.warning(
                "Consumer task for %s exited normally (unexpected for infinite loop)",
                agent_name,
            )
        # Attempt to recover by transitioning back to IDLE so the agent
        # can be restarted or polled again.
        if self._status.get(agent_name) not in (
            AgentState.SHUTTING_DOWN,
            AgentState.SHUTDOWN,
        ):
            self._transition(agent_name, AgentState.IDLE, reason="consumer_done_recover")

    # Watchdog: warn when dispatch exceeds this threshold (P0-a, seconds)
    _DISPATCH_WARN_SECONDS: float = 300.0

    def _get_dispatch_lock(self, agent_name: str) -> asyncio.Lock:
        return self._dispatch_locks.setdefault(agent_name, asyncio.Lock())

    def _bump_error_count(self, agent_name: str) -> int:
        """递增错误计数并返回当前值（上限受 _max_error_retries 限制）。"""
        error_count = self._error_counts.get(agent_name, 0)
        if error_count < self._max_error_retries:
            error_count += 1
            self._error_counts[agent_name] = error_count
        return error_count

    async def _maybe_backoff(self, agent_name: str, error_count: int) -> None:
        """根据错误计数执行退避睡眠；达到上限时停止 consume 循环并退出。"""
        if error_count >= self._max_error_retries:
            logger.error(
                "Agent %s exceeded max error retries (%d), stopping consumer",
                agent_name,
                self._max_error_retries,
            )
            self._transition(agent_name, AgentState.ERROR, reason="max_errors_exceeded")
            consumer_task = self._consumers.get(agent_name)
            if consumer_task is not None and not consumer_task.done():
                consumer_task.cancel()
        else:
            sleep_seconds = min(self._max_backoff_seconds, 2**error_count)
            logger.debug(
                "Agent %s backing off for %.1fs (error_count=%d)",
                agent_name,
                sleep_seconds,
                error_count,
            )
            await asyncio.sleep(sleep_seconds)

    async def _run_dispatch(self, agent_name: str, coro: Coroutine[Any, Any, None]) -> None:
        """包装 dispatch 协程，维护活跃计数和状态转换。

        consumer 循环快速 create_task，实际处理在后台执行，
        通过 per-session lock 保证同 session 串行，但不同 session 可以并发。
        """
        async with self._get_dispatch_lock(agent_name):
            self._active_session_counts[agent_name] = (
                self._active_session_counts.get(agent_name, 0) + 1
            )
            active_count = self._active_session_counts[agent_name]
        start_time = time.monotonic()
        if self._status.get(agent_name) != AgentState.WORKING:
            self._transition(agent_name, AgentState.WORKING, reason="dispatch_start")
        logger.debug(
            "Dispatch start: agent=%s active=%d",
            agent_name,
            active_count,
        )
        dispatch_timeout = self._safety.turn.dispatch_timeout_seconds
        try:
            if dispatch_timeout > 0:
                await asyncio.wait_for(coro, timeout=dispatch_timeout)
            else:
                await coro
            self._error_counts.pop(agent_name, None)
        except TimeoutError:
            elapsed = time.monotonic() - start_time
            logger.error(
                "Dispatch timeout for %s after %.1fs (threshold=%.0fs)",
                agent_name,
                elapsed,
                dispatch_timeout,
            )
            self._transition(agent_name, AgentState.ERROR, reason="dispatch_timeout")
            error_count = self._bump_error_count(agent_name)
            await self._maybe_backoff(agent_name, error_count)
        except Exception:
            # GraphInterrupt must propagate to the pipeline's approval handler;
            # do not treat it as a dispatch error.
            if isinstance(sys.exc_info()[1], GraphInterrupt):
                raise
            elapsed = time.monotonic() - start_time
            logger.exception(
                "Error dispatching message for %s (elapsed=%.1fs active=%d)",
                agent_name,
                elapsed,
                active_count,
            )
            self._transition(agent_name, AgentState.ERROR, reason="dispatch_error")
            error_count = self._bump_error_count(agent_name)
            await self._maybe_backoff(agent_name, error_count)
        finally:
            async with self._get_dispatch_lock(agent_name):
                current = self._active_session_counts.get(agent_name, 0)
                remaining = max(0, current - 1)
                self._active_session_counts[agent_name] = remaining
                elapsed = time.monotonic() - start_time
                if elapsed > self._DISPATCH_WARN_SECONDS:
                    logger.warning(
                        "Dispatch watchdog: agent=%s elapsed=%.1fs active=%d threshold=%.0fs",
                        agent_name,
                        elapsed,
                        remaining,
                        self._DISPATCH_WARN_SECONDS,
                    )
                if remaining == 0 and self._status.get(agent_name) not in (
                    AgentState.SHUTTING_DOWN,
                    AgentState.SHUTDOWN,
                ):
                    self._transition(agent_name, AgentState.IDLE, reason="dispatch_idle")

    async def _consume_messages(self, instance: AgentInstance, descriptor: AgentDescriptor) -> None:
        """常驻 Agent 的消息消费循环（基于消息类型的分发器）。"""
        address = descriptor.address
        while self._status.get(address.name) not in (AgentState.SHUTTING_DOWN, AgentState.SHUTDOWN):
            try:
                msg = await self._broker.consume(address)
                if msg is None:
                    continue

                logger.debug(
                    "AgentPool._consume_messages: %s received msg from=%s payload_keys=%s",
                    address.name,
                    msg.sender,
                    list(msg.payload.keys()),
                )

                # 1. Inbox wakeup 信号（同步顺序处理，不创建后台任务）
                if msg.payload.get("_inbox_wakeup"):
                    session_id = msg.payload.get("session_id", "")
                    if session_id:
                        logger.debug(
                            "AgentPool._consume_messages: %s handling inbox wakeup for %s",
                            address.name,
                            session_id,
                        )
                        await self._run_dispatch(
                            address.name,
                            self._handle_inbox_wakeup(instance, session_id),
                        )
                    continue

                # 2. 解析为 AgentMessageEnvelope 并后台分发
                envelope = AgentMessageEnvelope.from_broker_message(msg)
                if envelope is not None:
                    logger.debug(
                        "AgentPool._consume_messages: %s parsed envelope "
                        "type=%s source=%s target=%s session=%s",
                        address.name,
                        envelope.message_type,
                        envelope.source.name if envelope.source else None,
                        envelope.target.name if envelope.target else None,
                        envelope.agent_session_id,
                    )
                    if envelope.hop_count >= MAX_ENVELOPE_HOPS:
                        logger.warning(
                            "Dropping message for %s: hop_count %s exceeds limit %s",
                            address.name,
                            envelope.hop_count,
                            MAX_ENVELOPE_HOPS,
                        )
                        continue
                    if envelope.message_type == "task_request":
                        task = asyncio.create_task(
                            self._run_dispatch(
                                address.name,
                                self._dispatch_task_request(instance, descriptor, envelope),
                            )
                        )
                    else:
                        task = asyncio.create_task(
                            self._run_dispatch(
                                address.name,
                                self._dispatch_agent_message(instance, envelope),
                            )
                        )
                else:
                    logger.debug(
                        "AgentPool._consume_messages: %s could not parse envelope, "
                        "dispatching as raw broker message",
                        address.name,
                    )
                    task = asyncio.create_task(
                        self._run_dispatch(
                            address.name,
                            self._dispatch_raw_broker_message(instance, descriptor, msg),
                        )
                    )
                self._track_agent_task(address.name, task)
            except asyncio.CancelledError:
                break
            except GraphInterrupt:
                # Approval interrupt must propagate to the pipeline handler,
                # not be treated as a consumer-level error.
                raise
            except Exception:
                logger.exception("Error consuming messages for %s", address.name)
                self._transition(address.name, AgentState.ERROR, reason="consume_error")
                error_count = self._bump_error_count(address.name)
                if error_count >= self._max_error_retries:
                    logger.error(
                        "Agent %s exceeded max error retries (%d), stopping consumer",
                        address.name,
                        self._max_error_retries,
                    )
                    break
                sleep_seconds = min(self._max_backoff_seconds, 2**error_count)
                await asyncio.sleep(sleep_seconds)
                self._transition(address.name, AgentState.IDLE, reason="consume_recover")

    async def _handle_inbox_wakeup(
        self,
        instance: AgentInstance,
        session_id: str,
    ) -> None:
        """处理 Inbox 唤醒信号：轮询消息并分发。"""
        envelopes: list[AgentMessageEnvelope] = []
        if self._agent_bus is not None:
            envelopes = await self._agent_bus.poll(session_id, limit=10)
        elif self._inbox_consumer is not None:
            inbox_messages = await self._inbox_consumer.consume(session_id, limit=10)
            for inbox_msg in inbox_messages:
                envelopes.append(self._wrap_inbox_message(session_id, inbox_msg))
        else:
            logger.warning(
                "Received inbox wakeup for %s but no agent_bus or inbox_consumer configured",
                session_id,
            )
            return

        for envelope in envelopes:
            if envelope.hop_count >= MAX_ENVELOPE_HOPS:
                logger.warning(
                    "Dropping inbox message for %s: hop_count %s exceeds limit %s",
                    instance.descriptor.address.name,
                    envelope.hop_count,
                    MAX_ENVELOPE_HOPS,
                )
                continue
            agent_name = instance.descriptor.address.name
            if envelope.message_type == "task_request":
                await self._run_dispatch(
                    agent_name,
                    self._dispatch_task_request(instance, instance.descriptor, envelope),
                )
            else:
                await self._run_dispatch(
                    agent_name,
                    self._dispatch_agent_message(instance, envelope),
                )

    def _wrap_inbox_message(self, session_id: str, inbox_msg: InboxMessage) -> AgentMessageEnvelope:
        """将 InboxMessage 包装为 AgentMessageEnvelope，使用防御性字段提取。"""
        payload = inbox_msg.metadata.get("payload") if inbox_msg.metadata else None
        if payload is None:
            payload = {"content": inbox_msg.content, "message_type": inbox_msg.message_type}
        source_kind = payload.get("source_kind", "agent") if isinstance(payload, dict) else "agent"
        source_name = (
            payload.get("source", inbox_msg.source)
            if isinstance(payload, dict)
            else inbox_msg.source
        )
        return AgentMessageEnvelope(
            payload=payload,
            source=AgentAddress(kind=source_kind, name=source_name),
            message_type=payload.get("message_type", "subagent_result")
            if isinstance(payload, dict)
            else "subagent_result",
            conversation_id=inbox_msg.metadata.get("conversation_id", session_id)
            if inbox_msg.metadata
            else session_id,
            agent_session_id=inbox_msg.metadata.get("agent_session_id", session_id)
            if inbox_msg.metadata
            else session_id,
            message_id=inbox_msg.message_id,
            timestamp=inbox_msg.timestamp,
            metadata={
                k: v
                for k, v in (inbox_msg.metadata or {}).items()
                if k not in ("payload", "conversation_id")
            },
        )

    async def _dispatch_task_request(
        self,
        instance: AgentInstance,
        descriptor: AgentDescriptor,
        envelope: AgentMessageEnvelope,
    ) -> None:
        """将 task_request 信封转换为 InputMessage 并执行用户回合。"""
        task_prompt = envelope.payload.get("task_prompt") or envelope.payload.get("content", "")
        conversation_id = envelope.conversation_id or envelope.payload.get(
            "conversation_id", "default"
        )
        session_id = envelope.agent_session_id or self._session_strategy.agent_session(
            conversation_id, descriptor.address.name
        )
        metadata = {
            "conversation_id": conversation_id,
            "agent_session_id": session_id,
            "message_type": envelope.message_type,
            "source_agent": envelope.source.name if envelope.source else None,
            **envelope.metadata,
        }
        result: AgentResult | None = None
        if instance.pipeline is not None:
            lock = self.get_lock(session_id)
            async with lock:
                result = await instance.pipeline.process_message(
                    InputMessage(content=task_prompt, session_id=session_id, metadata=metadata)
                )

        # ephemeral agent: clear context after each turn
        if descriptor.context_strategy == "ephemeral" and instance.context_manager is not None:
            try:
                await instance.context_manager.clear(session_id)
            except Exception:
                logger.exception(
                    "Failed to clear ephemeral context for %s", descriptor.address.name
                )

        # send subagent_result back to parent
        if result is not None and envelope.message_type == "task_request":
            await self._send_subagent_result(descriptor, envelope, conversation_id, result)

    async def _send_subagent_result(
        self,
        descriptor: AgentDescriptor,
        envelope: AgentMessageEnvelope,
        conversation_id: str,
        result: AgentResult,
    ) -> None:
        """将 AgentResult 包装为 subagent_result 回传给父 Agent。"""
        parent_address = envelope.source
        parent_session_id = (
            self._session_strategy.agent_session(conversation_id, parent_address.name)
            if parent_address
            else conversation_id
        )

        result_envelope = AgentMessageEnvelope(
            payload={
                "content": result.content or "",
                "stop_reason": result.stop_reason,
                "partial_content": getattr(result, "partial_content", None),
                "error": getattr(result, "error", None),
            },
            source=descriptor.address,
            target=parent_address,
            message_type="subagent_result",
            conversation_id=conversation_id,
            agent_session_id=parent_session_id,
            correlation_id=envelope.correlation_id,
        )

        if self._agent_bus is not None:
            await self._agent_bus.send(parent_session_id, result_envelope)
        elif self._inbox_consumer is not None and hasattr(self._inbox_consumer, "_server"):
            producer = InboxProducer(server=self._inbox_consumer._server)
            await producer.send(parent_session_id, result_envelope)
            if self._broker is not None and parent_address is not None:
                await self._broker.send_to(
                    parent_address,
                    BrokerMessage(
                        payload={"_inbox_wakeup": True, "session_id": parent_session_id},
                        sender=AgentAddress(kind="system", name="agent_pool"),
                    ),
                )
        else:
            logger.warning(
                "Cannot send subagent_result for %s: no agent_bus or inbox_consumer available",
                descriptor.address.name,
            )

        # Channel 2: sync Future for create_and_wait() consumers
        correlation = envelope.correlation_id
        future = self._sync_futures.pop(correlation, None) if correlation else None
        if future is not None and not future.done():
            future.set_result(result)

    async def _dispatch_agent_message(
        self,
        instance: AgentInstance,
        envelope: AgentMessageEnvelope,
    ) -> None:
        """分发标准 agent_message（或 subagent_result）到 Agent Pipeline。"""
        conversation_id = envelope.conversation_id or envelope.payload.get(
            "conversation_id", "default"
        )
        session_id = envelope.agent_session_id or self._session_strategy.agent_session(
            conversation_id, instance.descriptor.address.name
        )
        content = envelope.payload.get("content", "")
        source_name = envelope.source.name if envelope.source else None
        target_name = envelope.target.name if envelope.target else None
        metadata = {
            "conversation_id": conversation_id,
            "agent_session_id": session_id,
            "message_type": envelope.message_type,
            "source_agent": source_name,
            "sender_agent": source_name,
            "receiver_agent": target_name,
            **envelope.metadata,
        }
        if instance.pipeline is not None:
            lock = self.get_lock(session_id)
            async with lock:
                await instance.pipeline.process_message(
                    InputMessage(content=content, session_id=session_id, metadata=metadata)
                )

    async def _dispatch_raw_broker_message(
        self,
        instance: AgentInstance,
        descriptor: AgentDescriptor,
        msg: BrokerMessage,
    ) -> None:
        """处理无法解析为 AgentMessageEnvelope 的原始 BrokerMessage。"""
        conversation_id = (
            msg.headers.get("conversation_id")
            or msg.payload.get("conversation_id")
            or msg.payload.get("session_id", "default")
        )
        session_id = msg.payload.get("agent_session_id") or self._session_strategy.agent_session(
            conversation_id, descriptor.address.name
        )
        content = msg.payload.get("content", "")
        metadata = {"conversation_id": conversation_id, "agent_session_id": session_id}
        if instance.pipeline is not None:
            lock = self.get_lock(session_id)
            async with lock:
                await instance.pipeline.process_message(
                    InputMessage(content=content, session_id=session_id, metadata=metadata)
                )

    async def register_directory(self, descriptor: AgentDescriptor) -> None:
        """将 Agent 注册到发现目录（不启动常驻 Pipeline）。"""
        self._status[descriptor.address.name] = AgentState.IDLE

    def get(self, name: str) -> AgentInstance | None:
        return self._agents.get(name)

    def get_descriptor(self, name: str) -> AgentDescriptor | None:
        instance = self._agents.get(name)
        return instance.descriptor if instance else None

    def get_status(self, name: str) -> AgentState:
        return self._status.get(name, AgentState.SHUTDOWN)

    def get_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session lock for pool-managed lifecycle and eviction.

        This is the **authoritative** concurrency guard for pool sessions.
        Session metadata (_session_meta), eviction decisions, and dispatch
        calls all acquire this lock to serialize access to a given session.

        AgentPipeline retains its own internal lock for direct (non-pool)
        callers. Pool code must NOT rely on the pipeline lock for lifecycle
        operations — use this lock instead.
        """
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    def _track_session(
        self, session_id: str, agent_name: str, is_dynamic: bool = False
    ) -> None:
        """Register new session metadata. Call inside lock-protected section."""
        self._session_meta[session_id] = SessionMeta(
            agent_name=agent_name,
            created_at=time.monotonic(),
            last_active=time.monotonic(),
            is_dynamic=is_dynamic,
        )

    def _touch_session(self, session_id: str) -> None:
        """Refresh activity timestamp. Call inside lock-protected section."""
        meta = self._session_meta.get(session_id)
        if meta:
            meta.last_active = time.monotonic()

    async def _try_evict_if_stale(self, session_id: str) -> None:
        """Evict a session if stale (TTL) OR if count exceeds per-subagent cap.

        Two policies:
        1. TTL: evict sessions inactive longer than ttl_seconds
        2. LRU count cap: when a subagent has > max_sessions_per_subagent
           sessions, evict the oldest (by created_at) first, regardless of TTL.

        Safety: acquires the session lock before making eviction decisions
        to eliminate the TOCTOU window between staleness check and eviction.
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            self._session_meta.pop(session_id, None)
            return
        try:
            await asyncio.wait_for(lock.acquire(), timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            return
        try:
            meta = self._session_meta.get(session_id)
            if meta is None:
                self._session_locks.pop(session_id, None)
                return
            if not meta.is_dynamic:
                return

            should_evict = False

            # Policy 1: TTL staleness
            if time.monotonic() - meta.last_active >= self._retention.ttl_seconds:
                should_evict = True

            # Policy 2: per-subagent count cap (LRU by created_at)
            if not should_evict:
                same_agent = [
                    (sid, m) for sid, m in self._session_meta.items()
                    if m.agent_name == meta.agent_name and m.is_dynamic
                ]
                if len(same_agent) > self._retention.max_sessions_per_subagent:
                    same_agent.sort(key=lambda x: x[1].created_at)
                    oldest_sid = same_agent[0][0]
                    if oldest_sid == session_id:
                        should_evict = True

            if not should_evict:
                return

            instance = self._agents.get(meta.agent_name)
            if instance and instance.context_manager:
                await instance.context_manager.clear(session_id)
            self._session_locks.pop(session_id, None)
            self._session_meta.pop(session_id, None)
        finally:
            lock.release()

    async def _cleanup_stale_sessions(self) -> None:
        """Background task: TTL eviction with concurrency safety."""
        while True:
            await asyncio.sleep(self._retention.cleanup_interval_seconds)
            # Per-agent session cap enforcement (LRU eviction)
            agents_seen: set[str] = {m.agent_name for m in self._session_meta.values()}
            for agent_name in agents_seen:
                await self._enforce_session_cap(agent_name)
            # TTL eviction
            for sid in list(self._session_meta.keys()):
                await self._try_evict_if_stale(sid)

    async def _enforce_session_cap(self, agent_name: str) -> None:
        """Ensure per-agent session count does not exceed cap.

        Evicts the least recently active dynamic sessions when the cap
        is exceeded. Resident (non-dynamic) sessions are not evicted
        by this mechanism.
        """
        cap = self._retention.max_sessions_per_subagent
        dynamic_sessions = sorted(
            ((sid, meta) for sid, meta in self._session_meta.items()
             if meta.agent_name == agent_name and meta.is_dynamic),
            key=lambda x: x[1].last_active,
        )
        excess = len(dynamic_sessions) - cap
        for sid, _meta in dynamic_sessions[:excess]:
            await self._try_evict_if_stale(sid)

    def list_agents(self) -> list[AgentDescriptor]:
        return [inst.descriptor for inst in self._agents.values()]

    def _make_profile(self, descriptor: AgentDescriptor) -> AgentProfile:
        status = self._status.get(descriptor.address.name, AgentState.SHUTDOWN)
        return AgentProfile(
            name=descriptor.address.name,
            role_description=descriptor.role_description,
            specialties=descriptor.specialties or None,
            status=status,
            allowed_tools=descriptor.allowed_tools,
            allowed_skills=descriptor.allowed_skills,
            capabilities=descriptor.address.capabilities or None,
            exposed_to_peers=descriptor.exposed_to_peers,
        )

    def _is_visible_to(self, descriptor: AgentDescriptor, caller: str | None) -> bool:
        if not descriptor.exposed_to_peers:
            return False
        if caller is None:
            return True
        if descriptor.allowed_callers is None:
            return True
        return caller in descriptor.allowed_callers

    def list_profiles(self, caller: str | None = None) -> list[AgentProfile]:
        return [
            self._make_profile(inst.descriptor)
            for inst in self._agents.values()
            if self._is_visible_to(inst.descriptor, caller)
        ]

    def get_profile(self, name: str) -> AgentProfile | None:
        instance = self._agents.get(name)
        if instance is None:
            return None
        return self._make_profile(instance.descriptor)

    def find_profiles(
        self,
        capability: str | None = None,
        skill: str | None = None,
        tool: str | None = None,
        caller: str | None = None,
    ) -> list[AgentProfile]:
        profiles = self.list_profiles(caller=caller)
        results: list[AgentProfile] = []
        for profile in profiles:
            # capability filter
            if capability is not None:
                caps = profile.capabilities or []
                if capability not in caps:
                    continue
            # skill filter
            if skill is not None:
                skills = profile.allowed_skills
                if skills is not None and skill not in skills:
                    continue
            # tool filter
            if tool is not None:
                tools = profile.allowed_tools
                if tools is not None and tool not in tools:
                    continue
            results.append(profile)
        return results

    async def _poll_inbox_for_idle_agents(self) -> None:
        """后台轮询 inbox：对 IDLE 状态的 agent，若其 session 有未读消息则发送 wakeup。"""
        while True:
            try:
                await asyncio.sleep(self._inbox_poll_interval)
                if self._agent_bus is None:
                    continue

                # 收集所有需要检查的 session：
                # 1. 已知 session（来自 _session_locks）
                # 2. inbox 中有 pending 消息的 session（覆盖从未处理过消息的 agent）
                sessions_to_check: set[str] = set(self._session_locks.keys())
                if self._inbox_consumer is not None:
                    try:
                        server = getattr(self._inbox_consumer, "_server", None)
                        if server is not None and hasattr(server, "list_sessions"):
                            inbox_sessions = await server.list_sessions()
                            sessions_to_check.update(inbox_sessions)
                    except Exception:
                        logger.debug("Failed to list inbox sessions", exc_info=True)

                for session_id in sessions_to_check:
                    _, agent_name = self._session_strategy.parse(session_id)
                    if not agent_name or agent_name not in self._agents:
                        continue
                    if self._status.get(agent_name) not in (AgentState.IDLE,):
                        continue
                    if not await self._agent_bus.has_pending(session_id):
                        continue
                    try:
                        await self._broker.send_to(
                            AgentAddress(kind="agent", name=agent_name),
                            BrokerMessage(
                                payload={"_inbox_wakeup": True, "session_id": session_id},
                                sender=AgentAddress(kind="system", name="agent_pool_inbox_poller"),
                            ),
                        )
                    except Exception:
                        logger.exception("Failed to send inbox wakeup poll for %s", session_id)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in inbox polling loop")
                await asyncio.sleep(self._inbox_poll_interval)

    async def shutdown_all(self, timeout: float = 10.0) -> None:
        if self._inbox_poll_task is not None:
            self._inbox_poll_task.cancel()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        for name in list(self._agents.keys()):
            self._transition(name, AgentState.SHUTTING_DOWN, reason="shutdown_all")
        for _, task in list(self._consumers.items()):
            task.cancel()
        if self._consumers:
            await asyncio.gather(*self._consumers.values(), return_exceptions=True)
        # 等待所有后台处理任务完成
        all_tasks: list[asyncio.Task] = []
        for tasks in list(self._agent_tasks.values()):
            all_tasks.extend(t for t in tasks if not t.done())
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        deadline = asyncio.get_running_loop().time() + timeout
        for name, instance in list(self._agents.items()):
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(instance.stop(), timeout=remaining)
            except TimeoutError:
                logger.warning("Agent %s did not shut down in time, forcing", name)
        self._agents.clear()
        self._consumers.clear()
        self._agent_tasks.clear()
        self._active_session_counts.clear()
        self._error_counts.clear()
        self._dispatch_locks.clear()
        self._session_locks.clear()
        for name in list(self._status.keys()):
            self._status[name] = AgentState.SHUTDOWN

    async def close(self, timeout: float = 10.0) -> None:
        """`shutdown_all` 的别名，提升 API 可发现性。"""
        await self.shutdown_all(timeout=timeout)
