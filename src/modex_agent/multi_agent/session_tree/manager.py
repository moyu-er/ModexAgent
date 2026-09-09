"""SessionTreeManager — runtime coordinator for the session-tree lifecycle."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.inbox.types import SessionWork
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.session_tree.models import (
    MessageTrack,
    MessageTrackStatus,
    NodeVersionStatus,
    SessionTreeMetadata,
    SessionTreeRecord,
    SessionTreeStatus,
    TreeNodeRecord,
)
from modex_agent.multi_agent.session_tree.request_scope import (
    REQUEST_SCOPE_ID_KEY,
    DispatchDecision,
    RequestBusyError,
    RequestOutcome,
    RequestReservation,
    RequestResult,
    RequestScopeError,
    RequestScopeRecord,
    RequestScopeState,
    RequestTurnFact,
    RequestTurnFactKind,
    ReservationLostError,
    ScopeMismatchError,
    ScopeRequiredError,
    is_terminal,
)
from modex_agent.multi_agent.session_tree.session_binding import SessionBinding
from modex_agent.utils.time import now_ms

if TYPE_CHECKING:
    from modex_agent.multi_agent.bus import LocalAgentMessageBus
    from modex_agent.multi_agent.envelope import AgentMessageEnvelope
    from modex_agent.multi_agent.inbox.types import InboxMessage
    from modex_agent.multi_agent.inbox_poller import InboxPoller
    from modex_agent.multi_agent.session_tree.session_binding import (
        SessionBindingStore,
    )
    from modex_agent.multi_agent.session_tree.store_node import TreeNodeStore
    from modex_agent.multi_agent.session_tree.store_track import MessageTrackStore
    from modex_agent.multi_agent.session_tree.store_tree import SessionTreeStore
    from modex_agent.persistence.session_registry import SessionRegistry

logger = logging.getLogger(__name__)

_PENDING_TYPES = frozenset({AgentMessageType.EXTERNAL_INPUT, AgentMessageType.AGENT_MESSAGE})
_TRACKED_TYPES = frozenset({AgentMessageType.TASK_REQUEST, AgentMessageType.AGENT_RESULT})

ApprovalTerminator = Callable[[Sequence[str]], Awaitable[None]]
"""Terminate each session's pending approval batch without running any tool.

Wired by the pool (the pipelines' owner) when the tree is attached; the tree
calls it once per cancelled scope. Never an LLM decision path.
"""


class _LiveRequestScope:
    """In-memory live owner of one request scope.

    The durable attribution fact is the serialized ``RequestScopeRecord`` in
    the root session's registry metadata; this object additionally carries
    the two-phase reservation and the latest root-turn facts, which only
    exist while THIS manager instance owns the scope.
    """

    __slots__ = ("record", "reservation", "root_result", "root_fail_reason", "root_handled")

    def __init__(self, record: RequestScopeRecord, reservation: RequestReservation) -> None:
        self.record = record
        self.reservation = reservation
        self.root_result: AgentResult | None = None
        self.root_fail_reason: str | None = None
        self.root_handled = False

    @property
    def tree_id(self) -> str:
        return self.record.tree_id


def _terminal_state_for(outcome: RequestOutcome) -> RequestScopeState:
    return {
        RequestOutcome.FINISHED: RequestScopeState.COMPLETED,
        RequestOutcome.HANDLED: RequestScopeState.COMPLETED,
        RequestOutcome.CANCELLED: RequestScopeState.CANCELLED,
        RequestOutcome.FAILED: RequestScopeState.FAILED,
        RequestOutcome.INTERRUPTED: RequestScopeState.INTERRUPTED,
    }[outcome]


class SessionTreeManager:

    def __init__(
        self,
        tree_store: SessionTreeStore,
        node_store: TreeNodeStore,
        track_store: MessageTrackStore,
        bus: LocalAgentMessageBus,
        poller: InboxPoller,
        pool_name: str,
        workspace_root: str,
        session_registry: SessionRegistry,
        binding_store: SessionBindingStore | None = None,
    ) -> None:
        self._tree_store = tree_store
        self._node_store = node_store
        self._track_store = track_store
        self._bus = bus
        self._poller = poller
        self._pool_name = pool_name
        self._workspace_root = workspace_root
        self._session_registry = session_registry
        self._bus.set_session_registry(session_registry)
        self._binding_store = binding_store
        self._running: set[str] = set()
        self._pending_input: set[str] = set()
        self._quiesce_events: dict[str, asyncio.Event] = {}
        self._paused_trees: set[str] = set()
        # ── Request-scope lifecycle (DESIGN.md §6, acp-adapter) ──
        self._scopes: dict[str, _LiveRequestScope] = {}
        self._scoped_sessions: set[str] = set()
        self._scoped_trees: set[str] = set()
        self._dispatch_scope: dict[str, str] = {}
        self._approval_terminator: ApprovalTerminator | None = None
        self._scope_cancel_tasks: dict[str, asyncio.Task[None]] = {}
        self._scope_admission_locks: dict[str, asyncio.Lock] = {}

    def _scope_admission_lock(self, tree_id: str) -> asyncio.Lock:
        return self._scope_admission_locks.setdefault(tree_id, asyncio.Lock())

    def _quiesce_event(self, tree_id: str) -> asyncio.Event:
        return self._quiesce_events.setdefault(tree_id, asyncio.Event())

    @property
    def binding_store(self) -> SessionBindingStore | None:
        return self._binding_store

    def _signal(self, tree_id: str) -> None:
        self._quiesce_event(tree_id).set()

    async def _maybe_bind_session(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Auto-create a SessionBinding from envelope metadata if not already bound.

        Called on every ``deliver``. Reads ``graph_instance_id`` from
        ``envelope.metadata`` and stores it as ``task_id`` in the binding store.
        Does NOT overwrite an existing binding — ``BotAgentNode.execute``
        creates a richer binding (with graph artifacts) before calling
        ``tree.deliver``, and that binding must survive subsequent delivers
        within the same session (e.g. subagent-reply wakeups).

        Raises ``ValueError`` if an existing binding's ``task_id`` conflicts
        with the envelope's ``graph_instance_id`` — this detects concurrent
        graph instances sharing a CACHED session, which would cross-contaminate
        graph contexts.
        """
        if self._binding_store is None:
            return
        existing = self._binding_store.get(session_id) or await self._persisted_binding(session_id)
        if existing is not None:
            incoming_gid = envelope.metadata.get("graph_instance_id")
            if incoming_gid is not None and existing.task_id is not None and incoming_gid != existing.task_id:
                raise ValueError(
                    f"Session {session_id!r} is bound to task_id={existing.task_id} "
                    f"but received a deliver with graph_instance_id={incoming_gid}. "
                    f"Concurrent graph instances sharing a CACHED session are not supported."
                )
            return
        task_id = envelope.metadata.get("graph_instance_id")
        if task_id is not None:
            await self.bind_session(
                session_id,
                SessionBinding(task_id=task_id),
            )

    async def bind_session(self, session_id: str, binding: SessionBinding) -> None:
        """Persist ownership before installing live, nonserializable artifacts.

        A persisted graph binding without its live root binding is nonrunnable,
        even if the process crashed before it could persist paused admission.
        """
        node = await self._ensure_node(session_id)
        existing = await self._persisted_binding(session_id)
        if existing is not None and existing.task_id != binding.task_id:
            record = await self._tree_store.get(node.tree_id)
            if record is not None and record.status != SessionTreeStatus.COMPLETED:
                raise ValueError(f"Session {session_id!r} still belongs to task {existing.task_id}")
        if binding.is_node_execution:
            # Binding restoration is part of entry, not permission to dispatch.
            self._paused_trees.add(node.tree_id)
        await self._session_registry.register(SessionInfo.from_str(session_id).model_copy(update={
            "metadata": {SessionTreeMetadata.BINDING: binding.model_dump(
                mode="json", exclude={"graph_artifacts"},
            )},
        }))
        if self._binding_store is not None:
            self._binding_store.bind(session_id, binding)

    async def _persisted_binding(self, session_id: str) -> SessionBinding | None:
        info = await self._session_registry.get(session_id)
        data = info.metadata.get(SessionTreeMetadata.BINDING) if info is not None else None
        return SessionBinding.model_validate(data) if data is not None else None

    async def find_paused_session(self, task_id: int, graph_node_name: str) -> SessionInfo | None:
        """Find the unfinished session whose durable admission requires node reentry."""
        for record in await self._tree_store.list_active():
            if await self.can_dispatch(record.root_node_session_id):
                continue
            binding = await self._persisted_binding(record.root_node_session_id)
            if binding is not None and (
                binding.task_id == task_id and binding.graph_node_name == graph_node_name
            ):
                return await self._session_registry.get(record.root_node_session_id)
        return None

    async def pending_work(self, session_id: str) -> SessionWork:
        return await self._bus.pending_work(session_id)

    def pending_sessions(self) -> set[str]:
        """Include reserved work no longer present in the MQ's pending index."""
        return self._pending_input.copy()

    # ── Request-scope lifecycle (DESIGN.md §6, acp-adapter) ─────────────
    #
    # The scope is an attribution fact inside THIS tree owner — never a
    # second executor, store, or completion signal: waiting delegates to
    # wait_quiesce (the one loop), cancellation delegates to the poller's
    # single-flight cancel + the same on_dispatch_end finalizer, and approval
    # stays with the original resumer/coordinator transaction.

    def attach_approval_terminator(self, terminator: ApprovalTerminator) -> None:
        """Wire the approval owner's no-LLM batch terminator (pool setter)."""
        self._approval_terminator = terminator

    async def register_request_scoped(self, session_id: str) -> None:
        """Fix the session's admission policy: prompts only via request scope."""
        self._scoped_sessions.add(session_id)
        await self._session_registry.register(SessionInfo.from_str(session_id).model_copy(
            update={"metadata": {SessionTreeMetadata.REQUEST_SCOPED: True}},
        ))

    async def is_request_scoped(self, session_id: str) -> bool:
        if session_id in self._scoped_sessions:
            return True
        info = await self._session_registry.get(session_id)
        if info is not None and info.metadata.get(SessionTreeMetadata.REQUEST_SCOPED) is True:
            self._scoped_sessions.add(session_id)
            return True
        return False

    async def _load_scope_record(self, session_id: str) -> RequestScopeRecord | None:
        info = await self._session_registry.get(session_id)
        data = info.metadata.get(SessionTreeMetadata.REQUEST_SCOPE) if info is not None else None
        return RequestScopeRecord.model_validate(data) if data is not None else None

    async def _persist_scope_record(
        self, record: RequestScopeRecord | None, *, session_id: str,
    ) -> None:
        """Write (or clear, with ``record=None``) the serialized scope fact."""
        await self._session_registry.register(SessionInfo.from_str(session_id).model_copy(
            update={"metadata": {
                SessionTreeMetadata.REQUEST_SCOPE: (
                    record.model_dump(mode="json") if record is not None else None
                ),
            }},
        ))

    async def _interrupt_scope_record(
        self, record: RequestScopeRecord, tree_id: str,
    ) -> None:
        """Settle a non-terminal persisted scope with no live owner.

        Restart settlement (DESIGN.md §6.5): the interrupted request's
        leftover tracks close and its leftover inbox work drains — never
        replayed into this or a later request. Ordinary records without a
        scope and graph/resident trees are untouched.
        """
        sessions = await self._node_store.get_tree_sessions(tree_id)
        # The SAME pool-owned approval terminator cancel uses: leftover
        # suspended snapshots are audited (REQUEST_CANCEL) and deleted, so a
        # restart survivor never leaves a resumable batch behind. A
        # termination failure reuses the cancel failure semantics — the scope
        # settles FAILED with the real reason and the error propagates (boot
        # recovery / the superseding begin_request must see it); the cleanup
        # defect is never swallowed into a clean INTERRUPTED.
        termination_error: Exception | None = None
        if self._approval_terminator is not None:
            try:
                await self._approval_terminator(sessions)
            except Exception as exc:
                termination_error = exc
                logger.exception("Approval termination failed while interrupting %s", tree_id)
        for sid in sessions:
            await self._track_store.close_tracks_for_session(sid, MessageTrackStatus.CANCELLED)
            self._pending_input.discard(sid)
            await self._drain_scope_inbox(sid)
        if termination_error is not None:
            await self._persist_scope_record(record.model_copy(update={
                "state": RequestScopeState.FAILED,
                "outcome": RequestOutcome.FAILED,
                "fail_reason": f"approval termination failed: {termination_error}",
                "updated_at": now_ms(),
            }), session_id=record.session_id)
            self._signal(tree_id)
            raise termination_error
        await self._persist_scope_record(record.model_copy(update={
            "state": RequestScopeState.INTERRUPTED,
            "outcome": RequestOutcome.INTERRUPTED,
            "updated_at": now_ms(),
        }), session_id=record.session_id)
        self._signal(tree_id)

    async def _transition_scope(self, live: _LiveRequestScope, record: RequestScopeRecord) -> None:
        """Gate dispatch during persistence, restoring admission if the write fails."""
        previous = live.record
        live.record = record
        try:
            await self._persist_scope_record(record, session_id=record.session_id)
        except BaseException:
            # A concurrent cancellation may already own a newer transition.
            if live.record is record:
                live.record = previous
            raise

    async def begin_request(self, session_id: str) -> RequestReservation:
        """Two-phase admission: issue the token nothing user-visible yet."""
        if not await self.is_request_scoped(session_id):
            raise ScopeRequiredError(
                f"Session {session_id!r} is not request-scoped; prompt admission unavailable"
            )
        node = await self._ensure_node(session_id)
        tree_id = node.tree_id
        async with self._scope_admission_lock(tree_id):
            # Graph/pause ownership closes request admission before any new
            # scope may open — a scoped session is never graph-bound.
            if await self._is_tree_paused(tree_id):
                raise RequestBusyError(f"Tree {tree_id!r} is paused; request admission closed")
            owner = await self._persisted_binding(tree_id)
            if owner is not None and owner.task_id is not None:
                raise RequestBusyError(
                    f"Tree {tree_id!r} is graph-bound to task {owner.task_id!r}"
                )
            live = self._scopes.get(tree_id)
            if live is not None and not is_terminal(live.record.state):
                raise RequestBusyError(
                    f"Session {session_id!r} already has live request {live.record.scope_id!r}"
                )
            persisted = await self._load_scope_record(session_id)
            if persisted is not None and not is_terminal(persisted.state):
                await self._interrupt_scope_record(persisted, tree_id)
            ts = now_ms()
            reservation = RequestReservation(
                scope_id=uuid.uuid4().hex, session_id=session_id, issued_at=ts,
            )
            record = RequestScopeRecord(
                scope_id=reservation.scope_id, session_id=session_id, tree_id=tree_id,
                state=RequestScopeState.RESERVED, created_at=ts, updated_at=ts,
            )
            # Install the live reservation BEFORE the awaited persistence: a
            # concurrent begin must see it immediately; persistence failure —
            # including cancellation mid-save — rolls the installation back.
            self._scopes[tree_id] = _LiveRequestScope(record, reservation)
            try:
                await self._persist_scope_record(record, session_id=session_id)
            except BaseException:
                self._scopes.pop(tree_id, None)
                raise
        self._signal(tree_id)
        return reservation

    async def release_reservation(self, reservation: RequestReservation) -> None:
        """Drop an unconsumed token; admission is immediately reusable."""
        node = await self._ensure_node(reservation.session_id)
        async with self._scope_admission_lock(node.tree_id):
            live = self._scopes.get(node.tree_id)
            if (
                live is None
                or live.record.scope_id != reservation.scope_id
                or live.record.state is not RequestScopeState.RESERVED
            ):
                return
            # Pop and clear under the SAME admission lock a concurrent begin
            # holds: the awaited clear can never overwrite a newer scope.
            self._scopes.pop(node.tree_id, None)
            await self._persist_scope_record(None, session_id=reservation.session_id)
        self._signal(node.tree_id)

    async def activate_reservation(self, reservation: RequestReservation) -> None:
        """Consume the token: scope RESERVED → ACTIVE before first delivery."""
        node = await self._ensure_node(reservation.session_id)
        async with self._scope_admission_lock(node.tree_id):
            live = self._scopes.get(node.tree_id)
            if (
                live is None
                or live.reservation != reservation
                or live.record.state is not RequestScopeState.RESERVED
            ):
                raise ReservationLostError(
                    f"Reservation {reservation.scope_id!r} is gone (released, consumed, or superseded)"
                )
            await self._transition_scope(
                live, live.record.with_state(RequestScopeState.ACTIVE, now_ms=now_ms()),
            )

    async def fail_scope(self, reservation: RequestReservation, fail_reason: str) -> None:
        """Settle a scope whose submission failed after activation."""
        node = await self._ensure_node(reservation.session_id)
        live = self._scopes.get(node.tree_id)
        if live is None or live.record.scope_id != reservation.scope_id:
            return
        if is_terminal(live.record.state):
            return
        await self._settle_scope(live, RequestOutcome.FAILED, fail_reason=fail_reason)

    async def note_scope_turn(self, session_id: str, fact: RequestTurnFact) -> None:
        """Record one dispatch turn's fact into the owning scope.

        Called by the pool inside the dispatch task, so a SUSPENDED fact is
        persisted (AWAITING_APPROVAL) BEFORE the poller's on_dispatch_end
        completion check can see the tree as drained. Child-session facts
        never overwrite the root fact — only the full child/track settlement
        lets the root's own last fact become the scope outcome.
        """
        node = await self._node_store.get(session_id)
        if node is None:
            return
        live = self._scopes.get(node.tree_id)
        if live is None or is_terminal(live.record.state):
            return
        if session_id != live.record.session_id:
            return
        if live.record.state is RequestScopeState.CANCELLING:
            # Cancellation owns settlement: a mid-cancel turn fact (a turn
            # that suspends while the finalizer drains) must never revive
            # the scope out of CANCELLING.
            return
        if fact.kind is RequestTurnFactKind.SUSPENDED:
            await self._transition_scope(live, live.record.model_copy(update={
                "state": RequestScopeState.AWAITING_APPROVAL,
                "pending_approval_turn_uuid": fact.pending_approval_turn_uuid,
                "updated_at": now_ms(),
            }))
            return
        if fact.kind is RequestTurnFactKind.FINISHED:
            live.root_result = fact.agent_result
            live.root_fail_reason = None
        elif fact.kind is RequestTurnFactKind.FAILED:
            live.root_fail_reason = fact.fail_reason or "turn failed"
        else:
            live.root_handled = True
        if live.record.state is RequestScopeState.AWAITING_APPROVAL:
            # The suspended batch concluded; the request is running again.
            await self._transition_scope(
                live, live.record.with_state(RequestScopeState.ACTIVE, now_ms=now_ms()),
            )

    async def wait_request(
        self, session_id: str, *, scope_id: str | None = None,
    ) -> RequestResult:
        """Wait for the session's current request scope to settle.

        Delegates to ``wait_quiesce`` — the SAME existing loop; a scope with
        a pending approval batch counts as pending work inside it, so the
        waiter keeps sleeping until the decision continuation drains. The
        terminal outcome is frozen (persisted) before this returns.

        ``scope_id`` is the waiter's correlation token (``run_input`` always
        passes its reservation's id): if the settled scope is a DIFFERENT
        request — this waiter was superseded — the mismatch is raised instead
        of returning another request's result.
        """
        node = await self._ensure_node(session_id)
        tree_id = node.tree_id
        while True:
            await self.wait_quiesce(tree_id)
            live = self._scopes.get(tree_id)
            if scope_id is not None and (live is None or live.record.scope_id != scope_id):
                raise ScopeMismatchError(
                    f"Waiter for scope {scope_id!r} was superseded on session {session_id!r}"
                )
            if live is None:
                return RequestResult(
                    scope_id="",
                    outcome=RequestOutcome.FAILED,
                    fail_reason=f"Session {session_id!r} has no live request scope",
                )
            if is_terminal(live.record.state):
                return self._scope_result(live)
            if live.record.state is RequestScopeState.CANCELLING:
                # Join the owned single-flight cancel finalizer; it settles
                # the scope and signals. Re-check through wait_quiesce — no
                # second wait loop here.
                task = self._scope_cancel_tasks.get(tree_id)
                if task is None:
                    return self._scope_result(live)
                await asyncio.shield(task)
                continue
            await self._settle_from_root_fact(live)
            return self._scope_result(live)

    async def cancel_request(self, session_id: str, scope_id: str) -> None:
        """Cancel one request scope; repeated cancels join the SAME finalizer."""
        node = await self._ensure_node(session_id)
        tree_id = node.tree_id
        inflight = self._scope_cancel_tasks.get(tree_id)
        if inflight is not None:
            await asyncio.shield(inflight)
            return
        live = self._scopes.get(tree_id)
        if live is None or is_terminal(live.record.state):
            return
        if live.record.scope_id != scope_id:
            raise ScopeMismatchError(
                f"Scope {scope_id!r} does not match live request {live.record.scope_id!r}"
            )
        task = asyncio.create_task(self._cancel_scope(tree_id, live))
        self._scope_cancel_tasks[tree_id] = task

        def clear_finished(finished: asyncio.Task[None]) -> None:
            if self._scope_cancel_tasks.get(tree_id) is finished:
                self._scope_cancel_tasks.pop(tree_id, None)

        task.add_done_callback(clear_finished)
        await asyncio.shield(task)

    async def settle_interrupted_scopes(self) -> int:
        """Restart settlement for persisted non-terminal scopes without a live owner.

        Runs before the poller's execution gate opens: leftover tracks and
        inbox work of the interrupted request are settled (never replayed),
        and the scope is frozen as INTERRUPTED. Ordinary records without a
        scope and graph/resident trees are untouched.
        """
        settled = 0
        for tree in await self._tree_store.list_active():
            if tree.tree_id in self._scopes:
                continue  # this process owns the scope
            persisted = await self._load_scope_record(tree.root_node_session_id)
            if persisted is None or is_terminal(persisted.state):
                continue
            await self._interrupt_scope_record(persisted, tree.tree_id)
            settled += 1
        return settled

    async def _settle_from_root_fact(self, live: _LiveRequestScope) -> None:
        if live.root_result is not None:
            await self._settle_scope(live, RequestOutcome.FINISHED, result=live.root_result)
        elif live.root_fail_reason is not None:
            await self._settle_scope(live, RequestOutcome.FAILED, fail_reason=live.root_fail_reason)
        elif live.root_handled:
            await self._settle_scope(live, RequestOutcome.HANDLED)
        else:
            await self._settle_scope(
                live, RequestOutcome.FAILED, fail_reason="request ended without a root result",
            )

    async def _settle_scope(
        self,
        live: _LiveRequestScope,
        outcome: RequestOutcome,
        *,
        result: AgentResult | None = None,
        fail_reason: str | None = None,
    ) -> None:
        """Freeze the terminal outcome (persist) BEFORE any waiter is woken."""
        if is_terminal(live.record.state):
            return
        terminal = live.record.model_copy(update={
            "state": _terminal_state_for(outcome),
            "outcome": outcome,
            "agent_result": result,
            "fail_reason": fail_reason,
            "pending_approval_turn_uuid": None,
            "updated_at": now_ms(),
        })
        await self._persist_scope_record(terminal, session_id=terminal.session_id)
        live.record = terminal
        self._signal(live.record.tree_id)

    def _scope_result(self, live: _LiveRequestScope) -> RequestResult:
        record = live.record
        return RequestResult(
            scope_id=record.scope_id,
            outcome=record.outcome or RequestOutcome.FAILED,
            agent_result=record.agent_result,
            fail_reason=record.fail_reason,
        )

    async def _cancel_scope(self, tree_id: str, live: _LiveRequestScope) -> None:
        # 1. Close admission first: the deliver gate rejects new prompts and
        #    can_dispatch stops starting turns while CANCELLING.
        await self._transition_scope(
            live, live.record.with_state(RequestScopeState.CANCELLING, now_ms=now_ms()),
        )
        self._signal(tree_id)
        # 2. The poller's single-flight cancel + the same per-turn finalizer.
        sessions = await self._node_store.get_tree_sessions(tree_id)
        await self._poller.cancel_sessions(sessions)
        # 3. The approval owner terminates the pending batch (audited as
        #    REQUEST_CANCEL) without any LLM run; late decisions find no
        #    snapshot and are stale. A termination failure must NOT surface
        #    as a clean cancel — settle FAILED and propagate.
        termination_error: Exception | None = None
        if self._approval_terminator is not None:
            try:
                await self._approval_terminator(sessions)
            except Exception as exc:
                termination_error = exc
                logger.exception("Approval termination failed while cancelling %s", tree_id)
        # 4. Settle the scope's inbox/track work: leftover tracks close and
        #    late child/provider results are archived — never dispatched
        #    into this or a later request. Session history is kept.
        for sid in sessions:
            await self._track_store.close_tracks_for_session(sid, MessageTrackStatus.CANCELLED)
            self._pending_input.discard(sid)
            await self._drain_scope_inbox(sid)
        # 5. Terminal outcome is frozen before the signal wakes the waiter.
        if termination_error is not None:
            await self._settle_scope(
                live,
                RequestOutcome.FAILED,
                fail_reason=f"approval termination failed: {termination_error}",
            )
            raise termination_error
        await self._settle_scope(live, RequestOutcome.CANCELLED)

    async def _drain_scope_inbox(self, session_id: str) -> None:
        """Full settlement of one session's inbox via the bus owner's primitives.

        Consumes until ``pending_work`` (which includes reserved entries)
        reports nothing left; the only bound is the bus's own consume batch,
        never an arbitrary fence here.
        """
        while True:
            batch = await self._bus.consume(session_id)
            if not batch:
                pending = await self._bus.pending_work(session_id)
                for message in pending.pending:
                    await self._bus.acknowledge(session_id, message.message_id)
                return
            for envelope in batch:
                await self._bus.acknowledge(session_id, envelope.message_id)

    async def _scoped_tree(self, tree_id: str) -> bool:
        """Request-scoped policy of the session tree (its ROOT's policy).

        Child causal messages inherit the owning tree's admission contract —
        checking only the target session itself would let messages addressed
        to a child bypass the scope.
        """
        if tree_id in self._scoped_trees:
            return True
        tree = await self._tree_store.get(tree_id)
        root_sid = tree.root_node_session_id if tree is not None else tree_id
        info = await self._session_registry.get(root_sid)
        if info is None or info.metadata.get(SessionTreeMetadata.REQUEST_SCOPED) is not True:
            return False
        self._scoped_trees.add(tree_id)
        return True

    async def sender_scope_id(self, session_id: str) -> str | None:
        """The request scope under which ``session_id``'s CURRENT turn runs.

        Source-time attribution: the common send path stamps outgoing causal
        messages with the SENDER's own running scope — never with the
        receiver's current scope, which would let a late reply from a
        superseded request masquerade as work of a newer one.
        """
        return self._dispatch_scope.get(session_id)

    async def _admit_scoped_input(
        self, session_id: str, tree_id: str, envelope: AgentMessageEnvelope,
    ) -> bool:
        """Deliver-time admission contract for request-scoped trees.

        Prompts (EXTERNAL_INPUT) need the current scope's token — even when
        the session is idle, no other entrance may bypass the scope — and a
        second prompt while a request is live is busy. Causal task/result
        carrier messages (TASK_REQUEST / AGENT_RESULT / AGENT_MESSAGE) must
        arrive stamped with their SOURCE's scope (SendStrategy stamps from
        the sender's running scope): a stamp matching the live request
        enters, anything else — unstamped, stale, or conflicting — is
        archived and never attributed to the receiver's current scope.
        """
        if not await self._scoped_tree(tree_id):
            return True
        scope = self._scopes.get(tree_id)
        stamped = envelope.metadata.get(REQUEST_SCOPE_ID_KEY)
        if envelope.message_type is AgentMessageType.EXTERNAL_INPUT:
            has_decision = envelope.payload.get("approval_decision") is not None
            if scope is None or is_terminal(scope.record.state):
                if has_decision:
                    raise ScopeMismatchError(
                        f"Session {session_id!r}: approval decision arrived after its request closed",
                    )
                raise ScopeRequiredError(
                    f"Session {session_id!r} is request-scoped; prompts need a request reservation",
                )
            if scope.record.state in (RequestScopeState.RESERVED, RequestScopeState.CANCELLING):
                if has_decision:
                    raise ScopeMismatchError(
                        f"Session {session_id!r}: request {scope.record.scope_id!r} is not accepting decisions",
                    )
                raise RequestBusyError(
                    f"Session {session_id!r} has an open request admission",
                )
            if stamped is not None and stamped != scope.record.scope_id:
                raise ScopeMismatchError(
                    f"Message carries scope {stamped!r} but session "
                    f"{session_id!r} request is {scope.record.scope_id!r}",
                )
            # A prompt without the live scope's token is a second prompt.
            if not has_decision and stamped != scope.record.scope_id:
                raise RequestBusyError(
                    f"Session {session_id!r} already has an active request; second prompt rejected",
                )
            return True
        # Causal carrier messages: source-time stamp is the ONLY admission.
        live = scope is not None and scope.record.state in (
            RequestScopeState.ACTIVE, RequestScopeState.AWAITING_APPROVAL,
        )
        if live and stamped == scope.record.scope_id:
            return True
        # Archived at delivery: unstamped (no running scope to vouch for it)
        # or stale/conflicting under the receiver's live request — it must
        # never inherit the receiver's current scope. ``False`` makes
        # ``deliver`` return before anything is enqueued or tracked.
        logger.info(
            "Archiving causal %s for %s (stamp=%s, live request=%s)",
            envelope.message_type,
            session_id,
            (stamped[:8] + "…") if isinstance(stamped, str) else None,
            scope.record.scope_id[:8] + "…" if live else None,
        )
        return False

    async def dispatch_consume_types(self, session_id: str) -> set[str] | None:
        """Consume filter for one poller dispatch batch.

        While a request scope is AWAITING_APPROVAL only decision prompts
        (EXTERNAL_INPUT) are consumed: causal carriers stay queued untouched —
        no hold/release churn — and a decision can never be starved behind
        held traffic no matter the batch size (the filter precedes the limit).
        """
        node = await self._node_store.get(session_id)
        if node is None:
            return None
        scope = self._scopes.get(node.tree_id)
        if scope is None or scope.record.state is not RequestScopeState.AWAITING_APPROVAL:
            return None
        return {AgentMessageType.EXTERNAL_INPUT.value}

    async def admit_scoped_dispatch(
        self, session_id: str, envelope: AgentMessageEnvelope,
    ) -> DispatchDecision:
        """Dispatch-time admission for one envelope in a request-scoped tree.

        The deliver gate admits messages into the inbox; this is the second
        half of the same contract at turn admission: already-persisted work
        is re-checked against the live scope, a suspended request's approval
        continuation goes before held child traffic, and stale carriers are
        archived instead of started as turns.
        """
        node = await self._node_store.get(session_id)
        if node is None:
            return DispatchDecision.DISPATCH
        scope = self._scopes.get(node.tree_id)
        if scope is None:
            self._dispatch_scope.pop(session_id, None)
            return DispatchDecision.DISPATCH
        stamped = envelope.metadata.get(REQUEST_SCOPE_ID_KEY)
        if stamped is not None and stamped != scope.record.scope_id:
            return DispatchDecision.ARCHIVE
        has_decision = envelope.payload.get("approval_decision") is not None
        if scope.record.state is RequestScopeState.AWAITING_APPROVAL:
            # The validated approval continuation goes before held traffic.
            if has_decision:
                self._dispatch_scope[session_id] = scope.record.scope_id
                return DispatchDecision.DISPATCH
            return DispatchDecision.HOLD
        if scope.record.state in (RequestScopeState.ACTIVE,):
            if stamped == scope.record.scope_id or has_decision:
                # Record the SOURCE scope this turn's outgoing causal sends
                # carry. A tagless message only passes as a validated
                # approval continuation; any other tagless work is held
                # (it cannot prove which request it belongs to).
                self._dispatch_scope[session_id] = scope.record.scope_id
                return DispatchDecision.DISPATCH
            return DispatchDecision.HOLD
        # RESERVED / CANCELLING: can_dispatch blocks the session anyway.
        return DispatchDecision.HOLD

    async def can_dispatch(self, session_id: str) -> bool:
        node = await self._node_store.get(session_id)
        if node is None:
            peeked = await self._bus.peek(session_id)
            node = await self._ensure_node(session_id, peeked[0] if peeked else None)
        record = await self._tree_store.get(node.tree_id)
        if record is not None and record.status == SessionTreeStatus.CANCELLED:
            return False
        owner = await self._persisted_binding(node.tree_id)
        if owner is not None and owner.task_id is not None:
            live = self._binding_store.get(node.tree_id) if self._binding_store is not None else None
            if live is None or live.task_id != owner.task_id:
                return False
            if owner.is_node_execution and live.graph_artifacts is None:
                return False
            if self._binding_store is not None and self._binding_store.get(session_id) is None:
                self._binding_store.bind(session_id, SessionBinding(task_id=owner.task_id))
        scope = self._scopes.get(node.tree_id)
        if scope is not None:
            # Only the live scope's own running/queued work dispatches
            # (ACTIVE turns, AWAITING_APPROVAL continuations). RESERVED,
            # CANCELLING and terminal scopes all block: late child/provider
            # results and stale prompts are archived, never executed into
            # this or a later request — a genuine next request opens a NEW
            # live scope, which reopens admission.
            if scope.record.state not in (
                RequestScopeState.ACTIVE, RequestScopeState.AWAITING_APPROVAL,
            ):
                return False
        else:
            # Live-owner gate (DESIGN.md §5.2): a persisted active/awaiting
            # scope with no in-process owner is a restart survivor — the
            # poller must not run its leftover tools before
            # settle_interrupted_scopes() has closed it out.
            persisted = await self._load_scope_record(
                record.root_node_session_id if record is not None else node.tree_id
            )
            if persisted is not None and not is_terminal(persisted.state):
                return False
        # Last check is synchronous with the poller's task admission. Pause
        # closes this gate before its first persistence await.
        return not await self._is_tree_paused(node.tree_id)

    async def pause_session(self, session_id: str) -> None:
        """Close the owning tree's admission, cancel its tasks, and drain cleanup."""
        node = await self._ensure_node(session_id)
        tree_id = node.tree_id
        self._paused_trees.add(tree_id)
        await self._session_registry.register(SessionInfo.from_str(tree_id).model_copy(update={
            "metadata": {SessionTreeMetadata.PAUSED: True},
        }))
        await self._tree_store.update_status(tree_id, SessionTreeStatus.ACTIVE)
        sessions = await self._node_store.get_tree_sessions(tree_id)
        await self._poller.cancel_sessions(sessions)
        self._signal(tree_id)
        await self.wait_quiesce(tree_id)

    async def is_session_paused(self, session_id: str) -> bool:
        node = await self._node_store.get(session_id)
        return node is not None and await self._is_tree_paused(node.tree_id)

    async def _is_tree_paused(self, tree_id: str) -> bool:
        info = await self._session_registry.get(tree_id)
        return tree_id in self._paused_trees or (
            info is not None and info.metadata.get(SessionTreeMetadata.PAUSED) is True
        )

    async def resume_session(self, session_id: str) -> bool:
        """Reopen only after live root binding restoration; return whether work remains."""
        node = await self._ensure_node(session_id)
        tree_id = node.tree_id
        sessions = await self._node_store.get_tree_sessions(tree_id)
        if any(sid in self._running for sid in sessions):
            raise RuntimeError("The owning tree must drain before resume")
        owner = await self._persisted_binding(tree_id)
        if owner is not None and owner.task_id is not None:
            live = self._binding_store.get(tree_id) if self._binding_store is not None else None
            if live is None or live.task_id != owner.task_id or (
                owner.is_node_execution and live.graph_artifacts is None
            ):
                raise RuntimeError("Restore the owning node's live binding before resuming its tree")
        await self.recover_tree(tree_id)
        pending = await self._has_pending_work(tree_id, sessions)
        await self._tree_store.update_status(tree_id, SessionTreeStatus.ACTIVE)
        await self._session_registry.register(SessionInfo.from_str(tree_id).model_copy(update={
            "metadata": {SessionTreeMetadata.PAUSED: False},
        }))
        self._paused_trees.discard(tree_id)
        self._poller.signal_wakeup()
        return pending

    async def unbind_session_tree(self, session_id: str) -> None:
        """Release runtime bindings after drain, retaining durable ownership."""
        node = await self._node_store.get(session_id)
        if node is not None and self._binding_store is not None:
            for sid in await self._node_store.get_tree_sessions(node.tree_id):
                self._binding_store.unbind(sid)

    async def _ensure_node(
        self, session_id: str, envelope: AgentMessageEnvelope | None = None
    ) -> TreeNodeRecord:
        existing = await self._node_store.get(session_id)
        if existing is not None:
            return existing
        parent_sid = envelope.parent_session_id if envelope is not None else None
        agent_name = envelope.target.name if envelope and envelope.target else ""
        if parent_sid is None or not agent_name:
            info = await self._session_registry.get(session_id)
            if info is not None:
                parent_sid = parent_sid or info.parent_session_id
                agent_name = agent_name or info.agent_name
        if parent_sid is not None:
            tree_id = (await self._ensure_node(parent_sid)).tree_id
        else:
            tree_id = session_id
            ts = now_ms()
            await self._tree_store.create(SessionTreeRecord(
                tree_id=tree_id, root_node_session_id=session_id, pool_name=self._pool_name,
                workspace_root=self._workspace_root, status=SessionTreeStatus.ACTIVE,
                created_at=ts, updated_at=ts,
            ))
        ts = now_ms()
        return await self._node_store.get_or_create(TreeNodeRecord(
            tree_id=tree_id, session_id=session_id, parent_session_id=parent_sid,
            agent_name=agent_name, version=0, parent_version=None,
            status=NodeVersionStatus.COMPLETED, created_at=ts, updated_at=ts,
        ))

    async def _send(self, target_session_id: str, envelope: AgentMessageEnvelope) -> bool:
        try:
            return await self._bus.send(target_session_id, envelope)
        except Exception:
            logger.exception("bus.send failed for %s", target_session_id)
            return False

    async def _scoped_send_failed(
        self, session_id: str, tree_id: str, message_id: str,
    ) -> bool:
        """Whether a rejected scoped send is a REAL failure, not a dedup.

        ``bus.send`` collapses dedup and error into ``False``; the bus owner's
        ``contains_pending`` separates them. A live-scope submission that is
        neither present nor persisted is a genuine failure the waiter must
        see — ordinary sessions keep the exact prior bool behavior.
        """
        scope = self._scopes.get(tree_id)
        if scope is None or is_terminal(scope.record.state):
            return False
        return not await self._bus.contains_pending(session_id, message_id)

    async def tree_id_for_session(self, session_id: str) -> str | None:
        """Return the tree containing ``session_id``, if one exists."""
        node = await self._node_store.get(session_id)
        return node.tree_id if node is not None else None

    async def deliver(
        self, target_session_id: str, envelope: AgentMessageEnvelope, *,
        track_consume: bool = False,
    ) -> None:
        msg_type = envelope.message_type
        node = await self._ensure_node(target_session_id, envelope)
        tree_id = node.tree_id

        await self._maybe_bind_session(target_session_id, envelope)
        # One admission contract for EVERY message type: prompts need the
        # scope token, causal task/result carrier messages are admitted only
        # with their SOURCE scope's stamp. An archived message ends delivery
        # here — nothing enqueued, nothing tracked.
        if not await self._admit_scoped_input(target_session_id, tree_id, envelope):
            return

        if msg_type in _PENDING_TYPES:
            self._pending_input.add(target_session_id)
        tracked_external_input = track_consume and msg_type is AgentMessageType.EXTERNAL_INPUT

        if msg_type in _PENDING_TYPES and not tracked_external_input:
            # NOTE: On _send failure (dedup or error), we discard from
            # _pending_input. This is correct for error (message not in inbox)
            # but technically wrong for dedup (message IS in inbox from a prior
            # delivery). The dedup case is unlikely (requires duplicate deliver
            # call) and self-corrects on next consume/dispatch. Changing would
            # require distinguishing dedup from error in bus.send's return type.
            if not await self._send(target_session_id, envelope):
                self._pending_input.discard(target_session_id)
                if await self._scoped_send_failed(
                    target_session_id, tree_id, envelope.message_id,
                ):
                    # A scoped prompt that was neither persisted nor already
                    # present is a REAL failure: the waiter must settle
                    # FAILED, not wait on a submission that never landed.
                    raise RequestScopeError(
                        f"Request submission for {target_session_id!r} failed"
                    )
            self._signal(tree_id)
            return

        if msg_type not in _TRACKED_TYPES and not tracked_external_input:
            await self._send(target_session_id, envelope)
            return

        task_req: MessageTrack | None = None
        if msg_type == AgentMessageType.AGENT_RESULT and envelope.invocation_id:
            for t in await self._track_store.list_dispatched(tree_id):
                if (
                    t.message_type == AgentMessageType.TASK_REQUEST
                    and t.invocation_id == envelope.invocation_id
                ):
                    task_req = t
                    await self._track_store.update_status(
                        t.track_id, MessageTrackStatus.CONSUMED, now_ms()
                    )
                    break

        if msg_type == AgentMessageType.TASK_REQUEST:
            source_sid = envelope.parent_session_id or ""
        else:
            source_sid = task_req.target_session_id if task_req is not None else ""
        track = MessageTrack(
            track_id=envelope.message_id,
            tree_id=tree_id,
            message_id=envelope.message_id,
            message_type=AgentMessageType(msg_type),
            invocation_id=envelope.invocation_id,
            target_session_id=target_session_id,
            source_session_id=source_sid,
            status=MessageTrackStatus.DISPATCHED,
            dispatched_at=now_ms(),
        )
        await self._track_store.create(track)

        if not await self._send(target_session_id, envelope):
            await self._track_store.update_status(
                track.track_id, MessageTrackStatus.CANCELLED
            )
            if tracked_external_input:
                self._pending_input.discard(target_session_id)
            if task_req is not None:
                await self._track_store.update_status(
                    task_req.track_id, MessageTrackStatus.DISPATCHED
                )
            if await self._scoped_send_failed(
                target_session_id, tree_id, envelope.message_id,
            ):
                # A scoped causal dispatch (task/request carrier) that never
                # landed is a REAL failure for the sending turn — surfaced
                # through the send strategy as a send error, never success.
                raise RequestScopeError(
                    f"Scoped {msg_type} delivery to {target_session_id!r} failed"
                )
        self._signal(tree_id)

    async def on_consumed(self, session_id: str, message: InboxMessage) -> None:
        msg_type = message.message_type
        if msg_type in _PENDING_TYPES:
            self._pending_input.discard(session_id)
            return
        if msg_type == AgentMessageType.TASK_REQUEST:
            return
        if msg_type == AgentMessageType.AGENT_RESULT:
            node = await self._ensure_node(session_id)
            tree_id = node.tree_id
            track = await self._track_store.get_by_message_id(
                tree_id, message.message_id
            )
            if track is not None:
                await self._track_store.update_status(
                    track.track_id, MessageTrackStatus.CONSUMED, now_ms()
                )
            self._signal(tree_id)

    async def on_dispatch_start(self, session_id: str) -> None:
        self._running.add(session_id)
        self._pending_input.discard(session_id)
        node = await self._ensure_node(session_id)
        await self._node_store.update_version(
            session_id,
            node.version + 1,
            node.version,
            NodeVersionStatus.RUNNING,
        )
        if node.tree_id not in self._paused_trees:
            await self._tree_store.update_status(node.tree_id, SessionTreeStatus.ACTIVE)

    async def on_dispatch_end(self, session_id: str, *, cancelled: bool = False) -> None:
        self._dispatch_scope.pop(session_id, None)
        await self._track_store.close_tracks_for_session(
            session_id, MessageTrackStatus.CANCELLED if cancelled else MessageTrackStatus.CONSUMED
        )
        node = await self._ensure_node(session_id)
        tree_id = node.tree_id
        await self._node_store.update_version(
            session_id,
            node.version,
            node.parent_version,
            NodeVersionStatus.CANCELLED if cancelled else NodeVersionStatus.COMPLETED,
        )
        if (await self.pending_work(session_id)).pending or await self._bus.peek(session_id):
            self._pending_input.add(session_id)
        else:
            self._pending_input.discard(session_id)
        record = await self._tree_store.get(tree_id)
        if (
            tree_id not in self._paused_trees
            and record is not None
            and record.status == SessionTreeStatus.ACTIVE
            and await self.is_quiesced(tree_id, finishing_session_id=session_id)
        ):
            await self._tree_store.update_status(tree_id, SessionTreeStatus.COMPLETED)
        self._running.discard(session_id)
        self._signal(tree_id)

    async def is_quiesced(self, tree_id: str, *, finishing_session_id: str | None = None) -> bool:
        sessions = await self._node_store.get_tree_sessions(tree_id)
        if any(s in self._running and s != finishing_session_id for s in sessions):
            return False
        if await self._is_tree_paused(tree_id):
            return True
        return not await self._has_pending_work(tree_id, sessions)

    async def _has_pending_work(self, tree_id: str, sessions: list[str]) -> bool:
        """Pending work survives pause even when the tree is quiescent for drain."""
        if await self._track_store.has_dispatched(tree_id):
            return True
        for sid in sessions:
            if (await self.pending_work(sid)).pending:
                return True
        if any(s in self._pending_input for s in sessions):
            return True
        # A request scope holding a pending approval batch keeps its waiter
        # inside the SAME quiesce loop until the decision continuation drains
        # (DESIGN.md §6.3). An immediate decision that raced the suspension
        # note is already visible above as pending input; single-flight keeps
        # its turn from starting until the suspended dispatch fully ends.
        scope = self._scopes.get(tree_id)
        return scope is not None and scope.record.state is RequestScopeState.AWAITING_APPROVAL

    async def wait_quiesce(self, tree_id: str) -> None:
        while True:
            event = self._quiesce_event(tree_id)
            event.clear()
            if await self.is_quiesced(tree_id):
                return
            self._poller.signal_wakeup()
            await event.wait()

    async def get_active_subtree_nodes(
        self, tree_id: str, session_id: str
    ) -> list[str]:
        """Return active session_ids in the subtree rooted at ``session_id``.

        Active = in ``_running``, in ``_pending_input``, or has a DISPATCHED
        track targeting it — the same three signals ``is_quiesced`` uses.
        Includes ``session_id`` itself. Uses ``get_tree_node_records`` (one
        query) + in-memory BFS over ``parent_session_id`` — no N+1 ``get(s)``.
        """
        records = await self._node_store.get_tree_node_records(tree_id)
        children_map: dict[str, list[str]] = {}
        for r in records:
            if r.parent_session_id is not None:
                children_map.setdefault(r.parent_session_id, []).append(r.session_id)
        descendants: set[str] = set()
        queue = [session_id]
        while queue:
            current = queue.pop(0)
            if current in descendants:
                continue
            descendants.add(current)
            queue.extend(children_map.get(current, []))
        tracks = await self._track_store.list_dispatched(tree_id)
        sessions_with_tracks = {t.target_session_id for t in tracks}
        return [
            s
            for s in descendants
            if s in self._running
            or s in self._pending_input
            or s in sessions_with_tracks
        ]

    async def on_session_evicted(self, session_id: str) -> None:
        self._dispatch_scope.pop(session_id, None)
        await self._track_store.close_tracks_for_session(
            session_id, MessageTrackStatus.CANCELLED
        )
        self._running.discard(session_id)
        self._pending_input.discard(session_id)
        if self._binding_store is not None:
            self._binding_store.unbind(session_id)
        node = await self._node_store.get(session_id)
        if node is None:
            return
        if node.parent_session_id is None:
            await self._tree_store.update_status(
                node.tree_id, SessionTreeStatus.CANCELLED
            )
            self._signal(node.tree_id)

    async def recover_tree(self, tree_id: str) -> None:
        for track in await self._track_store.list_dispatched(tree_id):
            if await self._bus.contains_pending(track.target_session_id, track.message_id):
                continue
            if track.message_type == AgentMessageType.AGENT_RESULT:
                await self._track_store.update_status(track.track_id, MessageTrackStatus.CONSUMED, now_ms())
            elif track.message_type == AgentMessageType.TASK_REQUEST:
                node = await self._node_store.get(track.target_session_id)
                if node is not None and node.status == NodeVersionStatus.RUNNING:
                    await self._node_store.update_version(
                        track.target_session_id, node.version, node.parent_version, NodeVersionStatus.CANCELLED)
                await self._track_store.update_status(track.track_id, MessageTrackStatus.CONSUMED, now_ms())
        sessions = await self._node_store.get_tree_sessions(tree_id)
        for sid in sessions:
            node = await self._node_store.get(sid)
            if node is not None and node.status == NodeVersionStatus.RUNNING:
                await self._node_store.update_version(sid, node.version, node.parent_version, NodeVersionStatus.CANCELLED)
        self._pending_input -= set(sessions)
        for sid in sessions:
            if (await self.pending_work(sid)).pending:
                self._pending_input.add(sid)
            for env in await self._bus.peek(sid, limit=100):
                if env.message_type in _PENDING_TYPES:
                    self._pending_input.add(sid)
                    break
