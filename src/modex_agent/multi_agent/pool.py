from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.multi_agent.context_fork import ContextForkBuilder
    from modex_agent.multi_agent.inbox_poller import InboxPoller
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.multi_agent.template import AgentTemplate
    from modex_agent.multi_agent.template_registry import AgentTemplateRegistry

from modex_agent.core.context import ContextManager
from modex_agent.core.graph.interrupt import GraphInterrupt
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionIdFactory, SessionInfo, session_id_prefix_of
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.core.types import InputMessage
from modex_agent.messaging.broker import AddressKind, MessageBroker
from modex_agent.messaging.broker_bridge import BrokerInputPayload
from modex_agent.runtime.dispatch import DispatchDeadline, current_dispatch_deadline

from .address import AgentAddress
from .bus import AgentMessageBus
from .descriptor import AgentDescriptor, AgentInstance
from .envelope import AgentMessageEnvelope
from .factory import AgentFactory
from .inbox.consumer import InboxConsumer
from .message_type import AgentMessageType
from .registry import AgentProfile, AgentRegistry
from .state import AgentState

logger = logging.getLogger(__name__)


@dataclass
class SessionRetentionPolicy:
    """Controls session cleanup for subagent task sessions."""

    max_sessions_per_subagent: int = 10
    max_sessions_global: int = 200
    ttl_seconds: float = 86400.0
    cleanup_interval_seconds: float = 1800.0


@dataclass(frozen=True)
class SessionActivity:
    """Per-session time signals for eviction.

    ``created_at`` is immutable session metadata (when the session was
    created) and is NEVER an eviction key. ``last_active`` is the TTL
    staleness signal, refreshed on every touch. LRU ordering is a separate
    int counter (``_session_lru``), the sole eviction sort key — keeping
    these three signals distinct prevents the created_at-vs-recency
    confusion that caused the candidate-② LRU bug.
    """

    created_at: float
    last_active: float


def input_message_from_dispatch_envelope(
    envelope: AgentMessageEnvelope,
    *,
    session: Any,
) -> InputMessage:
    """Backward-compat shim — prefer ``envelope.to_input_message(session=...)``.

    Retained because several integration test fixtures call this module-level
    function directly. New code should call ``envelope.to_input_message``
    (the metadata is built inside the envelope).
    """
    return envelope.to_input_message(session=session)


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
        session_factory: SessionIdFactory | None = None,
        safety: RuntimeSafetyPolicy | None = None,
        retention: SessionRetentionPolicy | None = None,
        session_registry: SessionRegistry | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self._agents: dict[str, AgentInstance] = {}
        self._status: dict[str, AgentState] = {}
        self._broker = broker
        self._agent_factory = agent_factory
        self._default_context_manager = default_context_manager
        self._agent_bus = agent_bus
        self._inbox_consumer = inbox_consumer
        self._session_factory = session_factory or SessionIdFactory()
        self._safety = safety or RuntimeSafetyPolicy()
        self._session_agents: dict[str, str] = {}
        self._session_activity: dict[str, SessionActivity] = {}
        self._session_lru_seq: int = 0
        self._session_lru: dict[str, int] = {}
        self._dynamic_sessions: set[str] = set()
        self._session_registry = session_registry
        self._session_store = session_store
        self._retention = retention or SessionRetentionPolicy()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[bool] | None = None
        self._agent_shutdown_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_session_counts: dict[str, int] = {}
        self._error_counts: dict[str, int] = {}
        self._max_error_retries: int = 5
        self._max_backoff_seconds: float = 10.0
        # ── ADR-0015 D3 lazy materialize (set by Task 2.9 wiring) ──
        self._materialize_deps: AgentMaterializeDeps | None = None
        self._template_registry: AgentTemplateRegistry | None = None
        self._pool_name: str | None = None
        # ── ADR-0015 D5 fork-context cleanup (set by Task 2.9 wiring) ──
        self._context_fork_builder: ContextForkBuilder | None = None
        # ── Per-poll InboxPoller (Task 7): attached by create_pool; started
        #    after materialize-deps injection; stopped in shutdown_all. ──
        self._poller: InboxPoller | None = None
        self._valid_transitions: dict[AgentState, set[AgentState]] = {
            AgentState.INITIALIZING: {AgentState.IDLE, AgentState.ERROR, AgentState.SHUTTING_DOWN},
            AgentState.IDLE: {AgentState.WORKING, AgentState.ERROR, AgentState.SHUTTING_DOWN},
            AgentState.WORKING: {AgentState.IDLE, AgentState.ERROR, AgentState.SHUTTING_DOWN},
            AgentState.ERROR: {AgentState.IDLE, AgentState.SHUTTING_DOWN},
            AgentState.SHUTTING_DOWN: {AgentState.SHUTDOWN},
            AgentState.SHUTDOWN: set(),
        }
        self._cleanup_task = asyncio.create_task(self._cleanup_stale_sessions())

    def _transition(self, name: str, new_state: AgentState, reason: str = "") -> None:
        current = self._status.get(name, AgentState.SHUTDOWN)
        if current == new_state:
            return
        valid = self._valid_transitions.get(current, set())
        if new_state not in valid:
            logger.warning(
                "Invalid state transition: %s -> %s for %s (reason=%s)",
                current.value,
                new_state.value,
                name,
                reason or "unspecified",
            )
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
        instance: AgentInstance,
    ) -> AgentInstance:
        """Register a pre-built AgentInstance (ADR-0015 D3).

        Instance construction moved to AgentTemplate.materialize; this method
        is now a thin store-and-register entry point. Between-turn driving is
        handled by the per-pool InboxPoller (Task 7).
        """
        name = descriptor.address.name
        self._transition(name, AgentState.INITIALIZING, reason="register_resident")
        self._agents[name] = instance
        self._transition(name, AgentState.IDLE, reason="register_resident_complete")
        return instance

    # Max envelopes consumed per drain cycle ( InboxPoller → consume_inbox ).
    _DRAIN_BATCH_LIMIT = 10

    # ── Per-pool InboxPoller ownership (Task 7) ──

    def attach_poller(self, poller: InboxPoller) -> None:
        """Attach this pool's InboxPoller (created by create_pool wiring)."""
        self._poller = poller

    @property
    def session_registry(self) -> SessionRegistry | None:
        """Expose the session registry for poller and wiring access."""
        return self._session_registry

    @property
    def materialize_deps(self) -> AgentMaterializeDeps | None:
        return self._materialize_deps

    @materialize_deps.setter
    def materialize_deps(self, value: AgentMaterializeDeps | None) -> None:
        self._materialize_deps = value

    @property
    def template_registry(self) -> AgentTemplateRegistry | None:
        return self._template_registry

    @template_registry.setter
    def template_registry(self, value: AgentTemplateRegistry | None) -> None:
        self._template_registry = value

    @property
    def pool_name(self) -> str | None:
        return self._pool_name

    @pool_name.setter
    def pool_name(self, value: str | None) -> None:
        self._pool_name = value

    @property
    def context_fork_builder(self) -> ContextForkBuilder | None:
        return self._context_fork_builder

    @context_fork_builder.setter
    def context_fork_builder(self, value: ContextForkBuilder | None) -> None:
        self._context_fork_builder = value

    def start_poller(self) -> None:
        """Start the attached poller, if any."""
        if self._poller is not None:
            self._poller.start()

    async def stop_poller(self) -> None:
        """Stop the attached poller, if any. Awaited from shutdown_all."""
        if self._poller is not None:
            await self._poller.stop()

    # ── Poll-driven unified inbox surface (Task 6) ──
    # These helpers are the InboxPoller's view of the pool: writers persist
    # only (submit_input), enumeration/consume are non-blocking, and
    # dispatch_envelope starts the turn. C2/C4/C5 in the redesign plan.

    async def submit_input(self, session_id: str, message: InputMessage) -> None:
        """Human DM / WebUI / approval → write external_input to this pool's inbox.

        Serializes the FULL InputMessage into the envelope payload using the
        BrokerInputPayload contract (C2) so the turn runner can reconstruct
        content + approval_decision + attachments_resolved + routing headers.
        Writers persist only — the poller starts the turn (P2).
        """
        payload_model = BrokerInputPayload(
            content=message.content,
            session_id=message.session.session_id_prefix,
            agent_session_id=session_id,
            metadata=dict(message.metadata) if message.metadata else {},
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            approval_decision=message.approval_decision.to_dict()
            if message.approval_decision is not None
            else None,
            attachments_resolved=[a.to_dict() for a in message.attachments_resolved],
            message_type=AgentMessageType.EXTERNAL_INPUT,  # extra field, allowed by extra="allow"
        )
        payload: dict[str, Any] = payload_model.model_dump(exclude_none=True)

        # Stamp the parent link when the target is a subagent session, so the
        # poller's dispatch path can read it from the envelope. submit_input
        # runs on the (workspace-bound) dispatcher path, so a single registry
        # lookup here is fine — it is NOT the per-turn hot path. Main-agent
        # targets carry no parent.
        parent_sid: str | None = None
        if self._session_registry is not None:
            child = await self._session_registry.get(session_id)
            if child is not None and child.parent_session_id:
                parent_sid = child.parent_session_id

        envelope = AgentMessageEnvelope(
            payload=payload,
            source=AgentAddress(kind=AddressKind.CHANNEL, name=message.source or "user"),
            target=AgentAddress(
                kind=AddressKind.AGENT, name=SessionInfo.from_str(session_id).agent_name
            ),
            message_type=AgentMessageType.EXTERNAL_INPUT,
            session_id=message.session.session_id_prefix,
            agent_session_id=session_id,
            parent_session_id=parent_sid,
        )
        if self._agent_bus is not None:
            await self._agent_bus.send(session_id, envelope)

    async def sessions_with_pending(self) -> list[str]:
        """Session ids that currently have ≥1 pending inbox message."""
        if self._agent_bus is not None:
            return await self._agent_bus.sessions_with_pending()
        if self._inbox_consumer is not None:
            return await self._inbox_consumer.sessions_with_pending()
        return []

    async def consume_inbox(
        self, session_id: str, *, only_types: set[str] | None = None
    ) -> list[AgentMessageEnvelope]:
        """Non-blocking consume of a batch of inbox envelopes for a session."""
        if self._agent_bus is not None:
            return await self._agent_bus.consume(
                session_id,
                limit=self._DRAIN_BATCH_LIMIT,
                only_types=only_types,
            )
        return []

    async def peek_inbox(
        self, session_id: str, limit: int = 1
    ) -> list[AgentMessageEnvelope]:
        """Non-destructive read of up to ``limit`` pending envelopes.

        Used by the InboxPoller to read the parent link off the first pending
        envelope WITHOUT consuming the batch — so a materialize failure still
        leaves the messages in the inbox.
        """
        if self._agent_bus is not None:
            return await self._agent_bus.peek(session_id, limit=limit)
        return []

    async def materialize_agent(
        self,
        session_id: str,
        template: AgentTemplate,
        *,
        parent_session_id: str | None = None,
    ) -> AgentInstance:
        """Materialize a subagent instance for ``session_id`` via ``template``.

        ``parent_session_id`` is the authoritative parent link, carried by the
        dispatching envelope and threaded in by the InboxPoller (it peeks the
        first pending envelope before materializing). Deriving the parent from
        the message — not recovering it from a workspace-partitioned session
        store — keeps subagent messaging independent of the active workspace.
        """
        parent = SessionInfo.from_str(parent_session_id) if parent_session_id else None
        inv_id = session_id_prefix_of(session_id)
        assert self._materialize_deps is not None  # set by pool wiring before poller runs
        return await template.materialize(parent, inv_id, self._materialize_deps)

    def get_template(self, agent_name: str) -> AgentTemplate | None:
        """Look up a materialization template for ``agent_name`` in this pool."""
        if self._template_registry is None or self._pool_name is None:
            return None
        return self._template_registry.get_template(self._pool_name, agent_name)

    async def dispatch_envelope(
        self,
        sid: str,
        instance: AgentInstance,
        envelope: AgentMessageEnvelope,
    ) -> None:
        """Run one turn for a drained inbox envelope (external_input or agent msg).

        Single reconstruction path: because ``submit_input`` writes a
        C2-compatible payload, this method handles BOTH ``external_input`` and
        inter-agent messages via the same ``envelope.to_input_message``.
        """
        if instance.pipeline is None:
            return
        agent_name = SessionInfo.from_str(sid).agent_name
        self._track_or_touch_session(sid, agent_name, envelope)
        session = self._stamp_session_from_envelope(sid, agent_name, envelope)
        await self._run_dispatch(
            agent_name,
            instance.pipeline.process_message(envelope.to_input_message(session=session)),
        )
        if envelope.invocation_id:
            await self._enforce_session_cap(agent_name)

    def _track_or_touch_session(
        self, sid: str, agent_name: str, envelope: AgentMessageEnvelope
    ) -> None:
        """Register new session metadata on first sight, else refresh activity."""
        if sid not in self._session_agents:
            self._track_session(sid, agent_name, is_dynamic=bool(envelope.invocation_id))
        else:
            self._touch_session(sid)

    @staticmethod
    def _stamp_session_from_envelope(
        sid: str, agent_name: str, envelope: AgentMessageEnvelope
    ) -> SessionInfo:
        """Rebuild the authoritative SessionInfo from the envelope.

        Parent link comes from the envelope (set by the dispatching producer),
        NOT recovered from a session store — this keeps subagent messaging
        independent of the active workspace. A TASK_REQUEST without a parent
        link is logged: result passback will be degraded.
        """
        session = SessionInfo.from_str(sid, default_agent_name=agent_name)
        if envelope.parent_session_id:
            return session.model_copy(
                update={"parent_session_id": envelope.parent_session_id}
            )
        if envelope.message_type == AgentMessageType.TASK_REQUEST:
            logger.warning(
                "dispatch_envelope: TASK_REQUEST for session %s carried no "
                "parent_session_id; ctx.session.parent_session_id will be None "
                "— result passback will be degraded.",
                sid,
            )
        return session

    # Watchdog: warn when dispatch exceeds this threshold (P0-a, seconds)
    _DISPATCH_WARN_SECONDS: float = 300.0

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
        else:
            sleep_seconds = min(self._max_backoff_seconds, 2**error_count)
            logger.debug(
                "Agent %s backing off for %.1fs (error_count=%d)",
                agent_name,
                sleep_seconds,
                error_count,
            )
            await asyncio.sleep(sleep_seconds)

    async def _run_dispatch(self, agent_name: str, coro: Coroutine[Any, Any, Any]) -> None:
        """Execute a turn coroutine with deadline watchdog + error recovery.

        ADR-0015 D6: the per-agent dispatch lock is removed — the Drainer is
        single-flight per session, so intra-session mutual exclusion is
        structural. Active-count decrement is a plain dict op.
        """
        self._active_session_counts[agent_name] = (
            self._active_session_counts.get(agent_name, 0) + 1
        )
        start_time = time.monotonic()
        current_state = self._status.get(agent_name)
        if current_state == AgentState.ERROR:
            self._error_counts.pop(agent_name, None)
            self._transition(agent_name, AgentState.IDLE, reason="error_recovery")
        if self._status.get(agent_name) != AgentState.WORKING:
            self._transition(agent_name, AgentState.WORKING, reason="dispatch_start")
        logger.debug(
            "Dispatch start: agent=%s active=%d",
            agent_name,
            self._active_session_counts.get(agent_name, 0),
        )
        dispatch_timeout = self._safety.turn.dispatch_timeout_seconds
        extension = self._safety.turn.agent_run_timeout_seconds
        deadline: DispatchDeadline | None = None
        watchdog_task: asyncio.Task[None] | None = None
        dispatch_task: asyncio.Task[None] | None = None
        token: Any = None
        try:
            if dispatch_timeout > 0:
                deadline = DispatchDeadline(
                    initial_timeout=dispatch_timeout,
                    extension=extension,
                )
                token = current_dispatch_deadline.set(deadline)
                dispatch_task = asyncio.ensure_future(coro)
                watchdog_task = asyncio.create_task(
                    self._dispatch_watchdog(dispatch_task, deadline),
                )
                try:
                    await dispatch_task
                except asyncio.CancelledError:
                    if deadline.is_expired:
                        raise TimeoutError from None
                    raise
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
                self._active_session_counts.get(agent_name, 0),
            )
            self._transition(agent_name, AgentState.ERROR, reason="dispatch_error")
            error_count = self._bump_error_count(agent_name)
            await self._maybe_backoff(agent_name, error_count)
        finally:
            if watchdog_task is not None and not watchdog_task.done():
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            if deadline is not None and token is not None:
                current_dispatch_deadline.reset(token)
            remaining = max(
                0, self._active_session_counts.get(agent_name, 1) - 1
            )
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

    # watchdog 最大轮询间隔：避免 sleep(remaining) 一次睡太久，
    # 导致对 renew() 的响应延迟过大。
    _WATCHDOG_POLL_INTERVAL: float = 5.0

    async def _dispatch_watchdog(
        self,
        task: asyncio.Task[None],
        deadline: DispatchDeadline,
    ) -> None:
        """监控 dispatch task 的可续期 deadline。过期则取消 task。"""
        try:
            while not task.done():
                remaining = deadline.remaining
                if remaining <= 0:
                    task.cancel()
                    return
                # 每轮最多睡 _WATCHDOG_POLL_INTERVAL，确保 renew() 后
                # 不需要等太久就能被感知到。
                await asyncio.sleep(min(remaining, self._WATCHDOG_POLL_INTERVAL))
        except asyncio.CancelledError:
            return

    def get(self, name: str) -> AgentInstance | None:
        return self._agents.get(name)

    def get_descriptor(self, name: str) -> AgentDescriptor | None:
        instance = self._agents.get(name)
        return instance.descriptor if instance else None

    def iter_instances(self) -> Iterator[AgentInstance]:
        """Yield every registered agent instance (main + subagents)."""
        yield from self._agents.values()

    def has_active_sessions(self) -> bool:
        """Return True if any agent has an in-progress dispatch.

        Used by workspace cd/exit to check whether switching is safe.
        """
        return any(count > 0 for count in self._active_session_counts.values())

    def get_status(self, name: str) -> AgentState:
        return self._status.get(name, AgentState.SHUTDOWN)

    def _track_session(self, session_id: str, agent_name: str, is_dynamic: bool = False) -> None:
        """Register new session metadata and persist via registry.

        ADR-0015 D6: session tracking no longer creates a per-session lock;
        intra-session mutual exclusion is structural via the single-flight
        Drainer. Registry registration is fire-and-forget.
        """
        now = time.monotonic()
        self._session_agents[session_id] = agent_name
        self._session_activity[session_id] = SessionActivity(
            created_at=now, last_active=now
        )
        self._session_lru_seq += 1
        self._session_lru[session_id] = self._session_lru_seq
        if is_dynamic:
            self._dynamic_sessions.add(session_id)
        if self._session_registry is not None:
            # Use from_str to recover the SessionInfo without re-encoding.
            # factory.create(external_id=session_id) would double-encode the
            # already-encoded prefix and produce a different session_id.
            session = SessionInfo.from_str(session_id, default_agent_name=agent_name)
            self._schedule_registry_register(session)

    def _touch_session(self, session_id: str) -> None:
        """Refresh activity timestamp. Call inside lock-protected section."""
        activity = self._session_activity.get(session_id)
        if activity is not None:
            self._session_activity[session_id] = replace(
                activity, last_active=time.monotonic()
            )
            self._session_lru_seq += 1
            self._session_lru[session_id] = self._session_lru_seq
        if self._session_registry is not None:
            self._fire_and_forget_registry(
                f"touch session {session_id}", self._session_registry.touch(session_id)
            )

    def _schedule_registry_register(self, session: SessionInfo) -> None:
        """Fire-and-forget registry registration with error logging."""
        if self._session_registry is None:
            return
        self._fire_and_forget_registry(
            f"register session {session}", self._session_registry.register(session)
        )

    def _fire_and_forget_registry(
        self, description: str, coro: Coroutine[Any, Any, None]
    ) -> None:
        """Schedule a registry operation as a background task, logging failures.

        The coroutine is created eagerly by the caller (already guarded by a
        ``None`` check); it only runs when the task is awaited. Errors are
        logged and never propagated — registry writes are best-effort.
        """

        async def _run() -> None:
            try:
                await coro
            except Exception:
                logger.exception("Failed to %s in registry", description)

        asyncio.create_task(_run())

    def _evict_session_tracking(self, session_id: str) -> None:
        """Remove all local tracking entries for a session."""
        self._session_agents.pop(session_id, None)
        self._session_activity.pop(session_id, None)
        self._session_lru.pop(session_id, None)
        self._dynamic_sessions.discard(session_id)

    async def _try_evict_if_stale(self, session_id: str) -> None:
        """Evict a session if stale by TTL.

        Clears the agent context and removes tracking. (The per-session
        Drainer coordination that used to live here was removed in Task 10;
        the between-turn driver is now the per-pool InboxPoller.)
        """
        if session_id not in self._session_agents:
            self._evict_session_tracking(session_id)
            return
        if session_id not in self._dynamic_sessions:
            return

        agent_name = self._session_agents[session_id]
        activity = self._session_activity.get(session_id)
        if activity is None:
            return
        if time.monotonic() - activity.last_active < self._retention.ttl_seconds:
            return

        instance = self._agents.get(agent_name)
        if instance and instance.context_manager:
            with contextlib.suppress(Exception):
                await instance.context_manager.clear(session_id)
        self._evict_session_tracking(session_id)

    async def _cleanup_stale_sessions(self) -> None:
        """Background task: TTL eviction with concurrency safety."""
        while True:
            await asyncio.sleep(self._retention.cleanup_interval_seconds)
            # Per-agent session cap enforcement (LRU eviction)
            agents_seen: set[str] = set(self._session_agents.values())
            for agent_name in agents_seen:
                await self._enforce_session_cap(agent_name)
            # TTL eviction
            for sid in list(self._session_agents.keys()):
                await self._try_evict_if_stale(sid)

    async def _enforce_session_cap(self, agent_name: str) -> None:
        """Ensure per-agent session count does not exceed cap.

        Evicts the least recently active dynamic sessions when the cap
        is exceeded. Resident (non-dynamic) sessions are not evicted
        by this mechanism.
        """
        cap = self._retention.max_sessions_per_subagent
        dynamic_sessions = sorted(
            (
                sid
                for sid in self._session_activity.keys()
                if self._session_agents.get(sid) == agent_name
                and sid in self._dynamic_sessions
            ),
            key=lambda sid: self._session_lru.get(sid, 0),
        )
        excess = len(dynamic_sessions) - cap
        if excess <= 0:
            return
        for sid in dynamic_sessions[:excess]:
            await self._evict_dynamic_session(sid)

    async def _evict_dynamic_session(self, session_id: str) -> None:
        """Evict a dynamic session selected by policy.

        Clears the agent context and the fork context, then removes tracking.
        (The per-session Drainer coordination that used to live here was
        removed in Task 10; the between-turn driver is now the per-pool
        InboxPoller.)
        """
        if session_id not in self._dynamic_sessions:
            self._evict_session_tracking(session_id)
            return
        agent_name = self._session_agents.get(session_id)
        if agent_name is not None:
            instance = self._agents.get(agent_name)
            if instance and instance.context_manager:
                with contextlib.suppress(Exception):
                    await instance.context_manager.clear(session_id)
        self._evict_session_tracking(session_id)

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
            exposed_to_agents=descriptor.exposed_to_agents,
            comm_kind=descriptor.comm_kind,
        )

    def _is_visible_to(self, descriptor: AgentDescriptor, caller: str | None) -> bool:
        if not descriptor.exposed_to_agents:
            return False
        if caller is None:
            return True
        if descriptor.allowed_callers is None:
            return True
        return caller in descriptor.allowed_callers

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
            if capability is not None:
                caps = profile.capabilities or []
                if capability not in caps:
                    continue
            if skill is not None:
                skills = profile.allowed_skills
                if skills is not None and skill not in skills:
                    continue
            if tool is not None:
                tools = profile.allowed_tools
                if tools is not None and tool not in tools:
                    continue
            results.append(profile)
        return results

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

    async def _shutdown_agent(self, agent_name: str) -> None:
        """Shut down a single agent and release its resources."""
        self._transition(agent_name, AgentState.SHUTTING_DOWN, reason="idle_cleanup")
        instance = self._agents.get(agent_name)
        if instance is not None:
            shutdown_task = self._agent_shutdown_tasks.get(agent_name)
            if shutdown_task is None:
                shutdown_task = asyncio.create_task(instance.stop())
                self._agent_shutdown_tasks[agent_name] = shutdown_task
            try:
                await asyncio.shield(shutdown_task)
            except asyncio.CancelledError:
                if (
                    shutdown_task.done()
                    and shutdown_task.cancelled()
                    and self._agent_shutdown_tasks.get(agent_name) is shutdown_task
                ):
                    self._agent_shutdown_tasks.pop(agent_name, None)
                raise
            except Exception:
                if self._agent_shutdown_tasks.get(agent_name) is shutdown_task:
                    self._agent_shutdown_tasks.pop(agent_name, None)
                return
            if self._agent_shutdown_tasks.get(agent_name) is shutdown_task:
                self._agent_shutdown_tasks.pop(agent_name, None)
            if self._agents.get(agent_name) is instance:
                self._agents.pop(agent_name, None)
                self._active_session_counts.pop(agent_name, None)
                self._error_counts.pop(agent_name, None)
            else:
                return
        self._transition(agent_name, AgentState.SHUTDOWN, reason="shutdown")
        logger.info("Agent %s shut down", agent_name)

    async def shutdown_all(self, timeout: float = 10.0) -> bool:
        shutdown_task = self._shutdown_task
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(self._shutdown_all_once(timeout))
            self._shutdown_task = shutdown_task
            shutdown_task.add_done_callback(self._clear_shutdown_task)
        return await asyncio.shield(shutdown_task)

    def _clear_shutdown_task(self, shutdown_task: asyncio.Task[bool]) -> None:
        if self._shutdown_task is shutdown_task:
            self._shutdown_task = None

    async def _shutdown_all_once(self, timeout: float) -> bool:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        # Task 7: stop the per-pool InboxPoller so no new between-turn
        # cycles start while agents are being torn down.
        await self.stop_poller()
        for name in list(self._agents.keys()):
            self._transition(name, AgentState.SHUTTING_DOWN, reason="shutdown_all")
        deadline = asyncio.get_running_loop().time() + timeout
        shutdown_owners = dict(self._agents)
        shutdown_tasks: dict[str, asyncio.Task[None]] = {}
        for name, instance in shutdown_owners.items():
            shutdown_task = self._agent_shutdown_tasks.get(name)
            if shutdown_task is None:
                shutdown_task = asyncio.create_task(instance.stop())
                self._agent_shutdown_tasks[name] = shutdown_task
            shutdown_tasks[name] = shutdown_task
        completed = True
        cancellation: asyncio.CancelledError | None = None
        for name, shutdown_task in shutdown_tasks.items():
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(asyncio.shield(shutdown_task), timeout=remaining)
            except TimeoutError:
                completed = False
                logger.warning("Agent %s shutdown timed out; retained for retry", name)
            except asyncio.CancelledError as exc:
                completed = False
                if (
                    shutdown_task.done()
                    and shutdown_task.cancelled()
                    and self._agent_shutdown_tasks.get(name) is shutdown_task
                ):
                    self._agent_shutdown_tasks.pop(name, None)
                logger.warning("Agent %s shutdown was cancelled; retained for retry", name)
                cancellation = exc
            except Exception:
                completed = False
                if self._agent_shutdown_tasks.get(name) is shutdown_task:
                    self._agent_shutdown_tasks.pop(name, None)
                logger.exception("Agent %s shutdown failed; retained for retry", name)
            else:
                if self._agent_shutdown_tasks.get(name) is shutdown_task:
                    self._agent_shutdown_tasks.pop(name, None)
                instance = shutdown_owners[name]
                if self._agents.get(name) is instance:
                    self._agents.pop(name, None)
                    self._active_session_counts.pop(name, None)
                    self._error_counts.pop(name, None)
                    self._transition(name, AgentState.SHUTDOWN, reason="shutdown_all_complete")
        if cancellation is not None:
            raise cancellation
        return completed
