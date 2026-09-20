"""Contract tests for the request-scope lifecycle (DESIGN.md §6, acp-adapter).

Real SessionTreeManager + AgentPool + InboxPoller + bus over in-memory
stores; the pipeline is a scripted stand-in at the LLM boundary (the real
ReAct+pool path is covered by the bot ACP harness suite). Contract under
test: two-phase admission, deliver-time gates, wait delegation to the
single quiesce loop, approval-held waiting, cancellation settlement, and
restart interruption — with ordinary (non-scoped) sessions untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker import AddressKind
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.messaging.models import ApprovalAction, ApprovalDecisionInput, InputMessage
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.session_tree.models import SessionTreeMetadata
from modex_agent.multi_agent.session_tree.request_scope import (
    REQUEST_SCOPE_ID_KEY,
    RequestBusyError,
    RequestOutcome,
    RequestScopeError,
    RequestScopeRecord,
    RequestScopeState,
    RequestTurnFact,
    RequestTurnFactKind,
    ScopeMismatchError,
    ScopeRequiredError,
)
from modex_agent.multi_agent.session_tree.store_node import InMemoryTreeNodeStore
from modex_agent.multi_agent.session_tree.store_track import InMemoryMessageTrackStore
from modex_agent.multi_agent.session_tree.store_tree import InMemorySessionTreeStore
from modex_agent.persistence.session_registry import InMemorySessionRegistry
from modex_agent.pipeline.turn_outcome import TurnOutcome, TurnSuspension
from modex_agent.tools.manager import InMemoryToolManager

ROOT_SID = "root.main"
CHILD_SID = "task1.helper"


class _ScriptedPipeline:
    """Stands in at the pipeline boundary; pool dispatch is the real call site."""

    def __init__(self) -> None:
        self.outcomes: list[TurnOutcome] = []
        self.seen: list[InputMessage] = []
        self.gate: asyncio.Event | None = None
        self.terminated = 0

    def push_finished(self, content: str) -> None:
        self.outcomes.append(TurnOutcome.finished(AgentResult(content=content)))

    def push_suspension(self, turn_uuid: str) -> None:
        self.outcomes.append(
            TurnOutcome.suspended(TurnSuspension(turn_uuid=turn_uuid, requests=[])),
        )

    async def process_message_outcome(self, input_msg: InputMessage) -> TurnOutcome:
        self.seen.append(input_msg)
        if self.gate is not None:
            await self.gate.wait()
        assert self.outcomes, "scripted pipeline has no queued outcome"
        return self.outcomes.pop(0)

    async def terminate_pending_approval(self, session_id: str) -> bool:
        self.terminated += 1
        return False


class _RaisingProducer(InboxProducer):
    """bus.send explodes — the scoped submission must surface a real failure."""

    async def send(self, session_id: str, envelope: object) -> bool:
        raise RuntimeError("broker down")


class _Harness:
    """Real pool + poller + tree manager over in-memory stores."""

    def __init__(self, producer: InboxProducer | None = None) -> None:
        self.registry = InMemorySessionRegistry()
        self.broker = InMemoryMessageBroker()
        self.inbox = InMemoryInboxServer()
        self.consumer = InboxConsumer(server=self.inbox)
        self.bus = LocalAgentMessageBus(
            producer=producer or InboxProducer(server=self.inbox),
            consumer=self.consumer,
        )
        self.pipeline = _ScriptedPipeline()
        self.pool = AgentPool(
            broker=self.broker,
            agent_factory=None,  # type: ignore[arg-type]
            agent_bus=self.bus,
            inbox_consumer=self.consumer,
            session_registry=self.registry,
        )
        self.descriptor = AgentDescriptor(
            address=AgentAddress(name="main"), system_prompt_template="t", max_iterations=1,
        )
        self.instance = AgentInstance(
            descriptor=self.descriptor,
            context_manager=InMemoryContextManager(),
            pipeline=self.pipeline,  # type: ignore[arg-type]
        )
        self.poller = InboxPoller(self.pool, interval=0.01, session_registry=self.registry)
        self.tree = SessionTreeManager(
            tree_store=InMemorySessionTreeStore(),
            node_store=InMemoryTreeNodeStore(),
            track_store=InMemoryMessageTrackStore(),
            bus=self.bus,
            poller=self.poller,
            pool_name="main",
            workspace_root=".",
            session_registry=self.registry,
        )
        self.consumer.set_on_consumed(self.tree.on_consumed)
        self.poller.attach_tree_manager(self.tree)
        self.pool.attach_poller(self.poller)
        self.pool.tree = self.tree  # setter wires the approval terminator

    def message(self, content: str = "hello") -> InputMessage:
        return InputMessage(
            content=content,
            session=SessionInfo.from_str(ROOT_SID),
            source="user",
            metadata={"message_id": content},
        )

    async def start(self) -> None:
        await self.pool.register_resident(self.descriptor, self.instance)
        self.pool.start_poller()

    async def aclose(self) -> None:
        await self.pool.stop_poller()
        await self.broker.stop()


class _AutoSendPipeline(_ScriptedPipeline):
    """Fires the REAL SubagentAutoSendHook at the outcome point of a turn.

    A real outcome-finally hook runs INSIDE the pool's dispatch coroutine;
    this stand-in reproduces exactly that timing so the source-scope stamp
    is exercised against the live dispatch state, not a mock.
    """

    def __init__(self, tree: SessionTreeManager, bus: LocalAgentMessageBus) -> None:
        super().__init__()
        self._tree = tree
        self._bus = bus
        self.during_dispatch_scope: str | None = None
        self.delivered: list[AgentMessageEnvelope] = []

    async def process_message_outcome(self, input_msg: InputMessage) -> TurnOutcome:
        self.seen.append(input_msg)
        hook = SubagentAutoSendHook(tree=self._tree, self_name="helper", parent_name="main")
        ctx = AgentContext(
            system_prompt="t",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo(
                session_id=CHILD_SID, agent_name="helper", parent_session_id=ROOT_SID,
            ),
            graph_instance_id=None,
        )
        await hook._notify_parent(ctx, session_id=CHILD_SID, content="child done")
        self.delivered.extend(await self._bus.peek(ROOT_SID))
        # The hook runs INSIDE the dispatch coroutine: the dispatch-time
        # source scope must still be live HERE (cleared only by
        # on_dispatch_end after the whole dispatch finishes).
        self.during_dispatch_scope = await self._tree.sender_scope_id(CHILD_SID)
        if self.gate is not None:
            await self.gate.wait()
        assert self.outcomes, "scripted pipeline has no queued outcome"
        return self.outcomes.pop(0)


class _AutoSendHarness(_Harness):
    """Adds a resident helper agent whose turn fires the real auto-send hook."""

    def __init__(self) -> None:
        super().__init__()
        self.helper_pipeline = _AutoSendPipeline(tree=self.tree, bus=self.bus)
        self.helper_descriptor = AgentDescriptor(
            address=AgentAddress(name="helper"), system_prompt_template="t", max_iterations=1,
        )
        self.helper_instance = AgentInstance(
            descriptor=self.helper_descriptor,
            context_manager=InMemoryContextManager(),
            pipeline=self.helper_pipeline,  # type: ignore[arg-type]
        )

    async def start(self) -> None:
        await super().start()
        await self.pool.register_resident(self.helper_descriptor, self.helper_instance)


def _scope_metadata(info: SessionInfo | None) -> RequestScopeRecord | None:
    data = info.metadata.get(SessionTreeMetadata.REQUEST_SCOPE) if info is not None else None
    return RequestScopeRecord.model_validate(data) if data is not None else None


async def _register_scoped(harness: _Harness) -> None:
    await harness.registry.register(SessionInfo.from_str(ROOT_SID))
    await harness.tree.register_request_scoped(ROOT_SID)


async def test_cancel_caller_cancellation_keeps_owned_finalizer() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    reservation = await harness.pool.begin_request(ROOT_SID)
    await harness.tree.activate_reservation(reservation)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def terminate(session_ids: object) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    harness.tree.attach_approval_terminator(terminate)
    first = asyncio.create_task(harness.tree.cancel_request(ROOT_SID, reservation.scope_id))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(harness.tree.cancel_request(ROOT_SID, reservation.scope_id))
        # Drain the second caller to the blocked finalizer before releasing it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await asyncio.wait_for(second, 1)
        result = await harness.tree.wait_request(ROOT_SID, scope_id=reservation.scope_id)
        assert result.outcome is RequestOutcome.CANCELLED
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await harness.aclose()


async def test_activation_save_failure_keeps_reservation_releasable(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness()
    await _register_scoped(harness)
    reservation = await harness.pool.begin_request(ROOT_SID)
    register = harness.registry.register

    async def fail_activation(info: SessionInfo) -> None:
        record = _scope_metadata(info)
        if record is not None and record.state is RequestScopeState.ACTIVE:
            raise OSError("activation persistence failed")
        await register(info)

    monkeypatch.setattr(harness.registry, "register", fail_activation)
    try:
        with pytest.raises(OSError, match="activation persistence failed"):
            await harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation)
        await harness.tree.release_reservation(reservation)
        replacement = await harness.pool.begin_request(ROOT_SID)
        assert replacement.scope_id != reservation.scope_id
        await harness.tree.release_reservation(replacement)
    finally:
        await harness.aclose()


async def test_materialize_deps_wires_approval_termination() -> None:
    harness = _Harness()
    # Reproduce assembly through materialize_deps, without the explicit tree setter.
    tree = SessionTreeManager(
        tree_store=InMemorySessionTreeStore(), node_store=InMemoryTreeNodeStore(),
        track_store=InMemoryMessageTrackStore(), bus=harness.bus, poller=harness.poller,
        pool_name="main", workspace_root=".", session_registry=harness.registry,
    )
    harness.pool.materialize_deps = AgentMaterializeDeps(
        agent_factory=None,  # type: ignore[arg-type]
        pool=harness.pool, session_factory=SessionIdFactory(),
        broker=harness.broker, tree=tree,
    )
    try:
        await harness.pool.register_resident(harness.descriptor, harness.instance)
        await tree.register_request_scoped(ROOT_SID)
        reservation = await harness.pool.begin_request(ROOT_SID)
        await tree.cancel_request(ROOT_SID, reservation.scope_id)
        assert harness.pipeline.terminated == 1
    finally:
        await harness.aclose()


async def test_terminal_save_failure_does_not_publish_success(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness()
    await _register_scoped(harness)
    reservation = await harness.pool.begin_request(ROOT_SID)
    await harness.tree.activate_reservation(reservation)
    await harness.tree.note_scope_turn(ROOT_SID, RequestTurnFact(
        kind=RequestTurnFactKind.FINISHED, agent_result=AgentResult(content="done"),
    ))
    register = harness.registry.register
    failed = False

    async def fail_first_terminal(info: SessionInfo) -> None:
        nonlocal failed
        record = _scope_metadata(info)
        if not failed and record is not None and record.state is RequestScopeState.COMPLETED:
            failed = True
            raise OSError("terminal persistence failed")
        await register(info)

    monkeypatch.setattr(harness.registry, "register", fail_first_terminal)
    try:
        with pytest.raises(OSError, match="terminal persistence failed"):
            await harness.tree.wait_request(ROOT_SID, scope_id=reservation.scope_id)
        result = await harness.tree.wait_request(ROOT_SID, scope_id=reservation.scope_id)
        persisted = _scope_metadata(await harness.registry.get(ROOT_SID))
        assert result.outcome is RequestOutcome.FINISHED
        assert persisted is not None and persisted.state is RequestScopeState.COMPLETED
    finally:
        await harness.aclose()


@pytest.mark.parametrize("state", [RequestScopeState.CANCELLING, RequestScopeState.AWAITING_APPROVAL])
async def test_failed_transition_does_not_leave_admission_closed(
    monkeypatch: pytest.MonkeyPatch, state: RequestScopeState,
) -> None:
    harness = _Harness()
    await _register_scoped(harness)
    reservation = await harness.pool.begin_request(ROOT_SID)
    await harness.tree.activate_reservation(reservation)
    register = harness.registry.register

    async def fail_transition(info: SessionInfo) -> None:
        record = _scope_metadata(info)
        if record is not None and record.state is state:
            raise OSError("transition persistence failed")
        await register(info)

    monkeypatch.setattr(harness.registry, "register", fail_transition)
    try:
        with pytest.raises(OSError, match="transition persistence failed"):
            if state is RequestScopeState.CANCELLING:
                await harness.tree.cancel_request(ROOT_SID, reservation.scope_id)
            else:
                await harness.tree.note_scope_turn(ROOT_SID, RequestTurnFact(
                    kind=RequestTurnFactKind.SUSPENDED, pending_approval_turn_uuid="approval-turn",
                ))
        assert await harness.tree.can_dispatch(ROOT_SID)
        assert await harness.tree.dispatch_consume_types(ROOT_SID) is None
    finally:
        monkeypatch.setattr(harness.registry, "register", register)
        await harness.tree.cancel_request(ROOT_SID, reservation.scope_id)
        await harness.aclose()


async def test_begin_requires_registered_policy() -> None:
    harness = _Harness()
    await harness.registry.register(SessionInfo.from_str(ROOT_SID))
    try:
        try:
            await harness.pool.begin_request(ROOT_SID)
        except ScopeRequiredError:
            pass
        else:
            raise AssertionError("begin must require the registered request-scoped policy")
    finally:
        await harness.aclose()


async def test_begin_release_allows_reuse_but_concurrent_begin_is_busy() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    try:
        first = await harness.pool.begin_request(ROOT_SID)
        try:
            await harness.pool.begin_request(ROOT_SID)
        except RequestBusyError:
            pass
        else:
            raise AssertionError("second begin over a live scope must be busy")
        await harness.tree.release_reservation(first)
        # Released scope left no persisted fact behind (assert BEFORE the
        # next begin replaces the record).
        assert _scope_metadata(await harness.registry.get(ROOT_SID)) is None
        second = await harness.pool.begin_request(ROOT_SID)
        assert second.scope_id != first.scope_id
        await harness.tree.release_reservation(second)
    finally:
        await harness.aclose()


async def test_run_input_returns_frozen_root_result() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.push_finished("done")
        result = await harness.pool.run_input(
            ROOT_SID, harness.message(), reservation=reservation,
        )
        assert result.scope_id == reservation.scope_id
        assert result.outcome is RequestOutcome.FINISHED
        assert result.agent_result is not None and result.agent_result.content == "done"
        record = _scope_metadata(await harness.registry.get(ROOT_SID))
        assert record is not None
        assert record.state is RequestScopeState.COMPLETED
        assert record.outcome is RequestOutcome.FINISHED
        assert record.agent_result is not None and record.agent_result.content == "done"
    finally:
        await harness.aclose()


async def test_run_input_rejects_reservation_target_mismatch() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        other = harness.message().model_copy(update={
            "session": SessionInfo.from_str("other.main"),
        })
        try:
            await harness.pool.run_input("other.main", other, reservation=reservation)
        except RequestScopeError:
            pass
        else:
            raise AssertionError("run_input must reject a target/reservation mismatch")
    finally:
        await harness.aclose()


async def test_scoped_session_rejects_tokenless_prompts() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        # IDLE: even a tokenless ordinary submit may not start a scoped session.
        try:
            await harness.pool.submit_input(ROOT_SID, harness.message())
        except ScopeRequiredError:
            pass
        else:
            raise AssertionError("tokenless prompt into a scoped session must be rejected")
        # Live request: a second prompt is busy, but the scope's own token works.
        reservation = await harness.pool.begin_request(ROOT_SID)
        try:
            await harness.pool.submit_input(ROOT_SID, harness.message())
        except RequestBusyError:
            pass
        else:
            raise AssertionError("second prompt over a live request must be busy")
        harness.pipeline.push_finished("ok")
        result = await harness.pool.run_input(
            ROOT_SID, harness.message(), reservation=reservation,
        )
        assert result.outcome is RequestOutcome.FINISHED
    finally:
        await harness.aclose()


async def test_pending_approval_holds_the_single_quiesce_loop() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.push_suspension("turn-1")
        wait_task = asyncio.create_task(
            harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation),
        )
        while not harness.pipeline.seen:
            await asyncio.sleep(0.01)
        record: RequestScopeRecord | None = None
        for _ in range(500):
            record = _scope_metadata(await harness.registry.get(ROOT_SID))
            if record is not None and record.state is RequestScopeState.AWAITING_APPROVAL:
                break
            await asyncio.sleep(0.01)
        assert record is not None and record.state is RequestScopeState.AWAITING_APPROVAL
        assert wait_task.done() is False, "waiter must keep sleeping while approval is pending"
        # The decision rides the SAME deliver path with approval_id exact match.
        decision = InputMessage(
            content="",
            session=SessionInfo.from_str(ROOT_SID),
            source="user",
            metadata={"message_id": "decision-1"},
            approval_decision=ApprovalDecisionInput(
                tool_call_id="call-1", action=ApprovalAction.ALLOW, approval_id="ap-1",
            ),
        )
        harness.pipeline.push_finished("resumed")
        await harness.pool.submit_input(ROOT_SID, decision)
        result = await asyncio.wait_for(wait_task, timeout=10)
        assert result.outcome is RequestOutcome.FINISHED
        assert result.agent_result is not None and result.agent_result.content == "resumed"
    finally:
        await harness.aclose()


async def test_cancel_settles_cancelled_and_admission_reopens() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.gate = asyncio.Event()
        wait_task = asyncio.create_task(
            harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation),
        )
        while not harness.pipeline.seen:
            await asyncio.sleep(0.01)
        cancel_task = asyncio.create_task(
            harness.tree.cancel_request(ROOT_SID, reservation.scope_id),
        )
        result = await asyncio.wait_for(wait_task, timeout=10)
        assert result.outcome is RequestOutcome.CANCELLED
        await asyncio.wait_for(cancel_task, timeout=10)
        record = _scope_metadata(await harness.registry.get(ROOT_SID))
        assert record is not None and record.state is RequestScopeState.CANCELLED
        assert harness.pipeline.terminated == 1
        # Repeated cancel joins the same finalizer and stays settled.
        await harness.tree.cancel_request(ROOT_SID, reservation.scope_id)
        # Admission reopens for a genuine next request.
        nxt = await harness.pool.begin_request(ROOT_SID)
        assert nxt.scope_id != reservation.scope_id
        await harness.tree.release_reservation(nxt)
    finally:
        await harness.aclose()


async def test_cancel_drains_held_inbox_work() -> None:
    """A live-scope carrier held behind a pending approval is drained by the
    cancel finalizer — never left pending for the next request."""
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.push_suspension("turn-1")
        wait_task = asyncio.create_task(
            harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation),
        )
        while not harness.pipeline.seen:
            await asyncio.sleep(0.01)
        await harness.tree.deliver(ROOT_SID, _carrier(
            AgentMessageType.AGENT_RESULT, ROOT_SID, stamp=reservation.scope_id,
        ))
        for _ in range(200):
            if await harness.pool.sessions_with_pending():
                break
            await asyncio.sleep(0.01)
        # Held, not dispatched: the suspended request's decision goes first.
        assert len(harness.pipeline.seen) == 1
        assert await harness.pool.sessions_with_pending() == [ROOT_SID]
        await harness.tree.cancel_request(ROOT_SID, reservation.scope_id)
        assert (await asyncio.wait_for(wait_task, timeout=10)).outcome is (
            RequestOutcome.CANCELLED
        )
        # The cancel finalizer drained the held work with the scope.
        assert await harness.pool.sessions_with_pending() == []
        assert await harness.bus.peek(ROOT_SID) == []
        # The suspended request's approval batch was terminated, not left pending.
        assert harness.pipeline.terminated == 1
    finally:
        await harness.aclose()


async def test_cancel_failure_is_not_reported_as_clean() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()

    async def _failing_terminator(session_ids: Any) -> None:
        raise RuntimeError("terminator exploded")

    harness.tree.attach_approval_terminator(_failing_terminator)
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        try:
            await harness.tree.cancel_request(ROOT_SID, reservation.scope_id)
        except RuntimeError as exc:
            assert "terminator exploded" in str(exc)
        else:
            raise AssertionError("cancel must not report false success")
        record = _scope_metadata(await harness.registry.get(ROOT_SID))
        assert record is not None
        assert record.outcome is RequestOutcome.FAILED
        assert record.fail_reason is not None and "terminator exploded" in record.fail_reason
    finally:
        await harness.aclose()


async def test_superseded_waiter_gets_scope_mismatch() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        first = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.push_finished("done")
        await harness.pool.run_input(ROOT_SID, harness.message(), reservation=first)
        second = await harness.pool.begin_request(ROOT_SID)
        try:
            await harness.tree.wait_request(ROOT_SID, scope_id=first.scope_id)
        except ScopeMismatchError:
            pass
        else:
            raise AssertionError("a superseded waiter must get a scope mismatch")
        await harness.tree.release_reservation(second)
    finally:
        await harness.aclose()


async def test_settle_interrupted_scopes_closes_restart_survivor() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    reservation = await harness.pool.begin_request(ROOT_SID)
    # The prompt of the doomed request was already delivered pre-restart,
    # stamped with its scope attribution like run_input stamps it.
    await harness.tree.activate_reservation(reservation)
    await harness.pool.submit_input(ROOT_SID, harness.message("lost-prompt").model_copy(
        update={"metadata": {
            "message_id": "lost-prompt",
            REQUEST_SCOPE_ID_KEY: reservation.scope_id,
        }},
    ))
    # Simulate the restart: a fresh manager with no live scope objects, same
    # registry + bus (the persisted ACTIVE scope is its only trace).
    fresh_poller = InboxPoller(harness.pool, interval=0.01, session_registry=harness.registry)
    fresh_tree = SessionTreeManager(
        tree_store=harness.tree._tree_store,
        node_store=harness.tree._node_store,
        track_store=harness.tree._track_store,
        bus=harness.bus,
        poller=fresh_poller,
        pool_name="main",
        workspace_root=".",
        session_registry=harness.registry,
    )
    fresh_poller.attach_tree_manager(fresh_tree)
    harness.pool.attach_poller(fresh_poller)
    harness.pool.tree = fresh_tree
    settled = await fresh_tree.settle_interrupted_scopes()
    assert settled == 1
    record = _scope_metadata(await harness.registry.get(ROOT_SID))
    assert record is not None
    assert record.state is RequestScopeState.INTERRUPTED
    assert record.outcome is RequestOutcome.INTERRUPTED
    # Leftover inbox work was drained, not replayed.
    assert await harness.pool.sessions_with_pending() == []
    # And the fresh tree can accept a new request immediately.
    nxt = await fresh_tree.begin_request(ROOT_SID)
    await fresh_tree.release_reservation(nxt)
    await harness.aclose()


async def test_ordinary_sessions_keep_prior_semantics() -> None:
    harness = _Harness()
    plain = "plain.main"
    await harness.registry.register(SessionInfo.from_str(plain))
    try:
        # No register_request_scoped: tokenless submit behaves exactly as before.
        await harness.pool.submit_input(plain, harness.message("ordinary"))
        assert plain in await harness.pool.sessions_with_pending()
        try:
            await harness.pool.begin_request(plain)
        except ScopeRequiredError:
            pass
        else:
            raise AssertionError("ordinary sessions have no request admission")
    finally:
        await harness.aclose()


def _carrier(
    msg_type: str,
    target_sid: str,
    *,
    stamp: str | None = None,
) -> AgentMessageEnvelope:
    return AgentMessageEnvelope(
        payload={"content": "carrier"},
        source=AgentAddress(kind=AddressKind.AGENT, name="main"),
        target=AgentAddress(kind=AddressKind.AGENT, name="helper"),
        message_type=msg_type,
        session_id="conv1",
        agent_session_id=target_sid,
        parent_session_id=ROOT_SID if msg_type == AgentMessageType.TASK_REQUEST else target_sid,
        metadata={REQUEST_SCOPE_ID_KEY: stamp} if stamp is not None else {},
    )


async def test_task_request_carries_source_scope_not_receiver_scope() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.gate = asyncio.Event()
        wait_task = asyncio.create_task(
            harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation),
        )
        while not harness.pipeline.seen:
            await asyncio.sleep(0.01)
        # Source-time attribution: the ROOT's RUNNING scope stamps its
        # outgoing TASK_REQUEST (the seam SendStrategy consumes).
        scope_id = await harness.tree.sender_scope_id(ROOT_SID)
        assert scope_id == reservation.scope_id
        child_sid = "task1.helper"
        await harness.tree.deliver(child_sid, _carrier(
            AgentMessageType.TASK_REQUEST, child_sid, stamp=scope_id,
        ))
        peeked = await harness.bus.peek(child_sid)
        assert peeked, "stamped carrier of the live request must be delivered"
        assert peeked[0].metadata.get(REQUEST_SCOPE_ID_KEY) == reservation.scope_id
        # Cleanup through the cancel finalizer (the gated turn never ends).
        await harness.tree.cancel_request(ROOT_SID, reservation.scope_id)
        assert (await asyncio.wait_for(wait_task, timeout=5)).outcome is (
            RequestOutcome.CANCELLED
        )
        # Turn ended: the sender no longer runs under any scope.
        assert await harness.tree.sender_scope_id(ROOT_SID) is None
    finally:
        await harness.aclose()


async def test_late_untagged_reply_is_archived_not_stamped_with_new_scope() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        first = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.push_finished("done")
        await harness.pool.run_input(ROOT_SID, harness.message(), reservation=first)
        second = await harness.pool.begin_request(ROOT_SID)
        # A late child reply with NO source stamp arrives while a newer
        # request holds the session: archived, never attributed to it.
        await harness.tree.deliver(ROOT_SID, _carrier(
            AgentMessageType.AGENT_RESULT, ROOT_SID, stamp=None,
        ))
        assert await harness.bus.peek(ROOT_SID) == []
        record = _scope_metadata(await harness.registry.get(ROOT_SID))
        assert record is not None and record.scope_id == second.scope_id
        await harness.tree.release_reservation(second)
    finally:
        await harness.aclose()


async def test_stale_stamped_reply_archived_under_newer_request() -> None:
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        first = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.push_finished("done")
        await harness.pool.run_input(ROOT_SID, harness.message(), reservation=first)
        second = await harness.pool.begin_request(ROOT_SID)
        await harness.tree.deliver(ROOT_SID, _carrier(
            AgentMessageType.AGENT_RESULT, ROOT_SID, stamp=first.scope_id,
        ))
        assert await harness.bus.peek(ROOT_SID) == [], "stale stamp must not enter"
        await harness.tree.release_reservation(second)
    finally:
        await harness.aclose()


async def test_scoped_submission_error_is_real_failure_not_wait() -> None:
    harness = _Harness(producer=_RaisingProducer(server=InMemoryInboxServer()))
    await _register_scoped(harness)
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.push_finished("never")
        try:
            await harness.pool.run_input(
                ROOT_SID, harness.message(), reservation=reservation,
            )
        except RequestScopeError:
            pass
        else:
            raise AssertionError("a failed scoped submission must raise, not wait")
        record = _scope_metadata(await harness.registry.get(ROOT_SID))
        assert record is not None
        assert record.state is RequestScopeState.FAILED
        assert record.outcome is RequestOutcome.FAILED
        assert record.fail_reason is not None and "submission failed" in record.fail_reason
    finally:
        await harness.aclose()


async def test_auto_send_hook_stamps_within_dispatch_before_scope_cleared() -> None:
    """Real dispatch timing: the outcome-finally hook reads the SENDER's
    running scope DURING its dispatch (admit_scoped_dispatch set it, and only
    on_dispatch_end — after the whole dispatch coroutine — clears it).

    The stamped AGENT_RESULT is admitted by the parent's scoped deliver gate;
    a post-dispatch refire is unstamped and archived under the closed scope.
    """
    harness = _AutoSendHarness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.gate = asyncio.Event()  # root turn in flight
        harness.pipeline.push_finished("done")
        harness.pipeline.push_finished("ack")  # root turn for the child result
        harness.helper_pipeline.push_finished("child ack")
        wait_task = asyncio.create_task(
            harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation),
        )
        while not harness.pipeline.seen:
            await asyncio.sleep(0.01)
        await harness.tree.deliver(CHILD_SID, _carrier(
            AgentMessageType.TASK_REQUEST, CHILD_SID, stamp=reservation.scope_id,
        ))
        while harness.helper_pipeline.during_dispatch_scope is None:
            await asyncio.sleep(0.01)
        # The hook fired mid-dispatch: the source scope was still live.
        assert harness.helper_pipeline.during_dispatch_scope == reservation.scope_id
        # The auto-sent AGENT_RESULT carries the stamp and the parent's
        # scoped deliver gate ADMITTED it (visible in the parent's inbox).
        assert harness.helper_pipeline.delivered, "stamped child result must be admitted"
        (delivered,) = harness.helper_pipeline.delivered
        assert delivered.metadata[REQUEST_SCOPE_ID_KEY] == reservation.scope_id

        harness.pipeline.gate.set()
        result = await asyncio.wait_for(wait_task, timeout=5)
        assert result.outcome is RequestOutcome.FINISHED

        # Dispatch fully ended: the source scope is cleared — a late hook
        # fire carries no stamp and the closed scope archives the reply.
        assert await harness.tree.sender_scope_id(CHILD_SID) is None
        hook = SubagentAutoSendHook(
            tree=harness.tree, self_name="helper", parent_name="main",
        )
        ctx = AgentContext(
            system_prompt="t",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo(
                session_id=CHILD_SID, agent_name="helper", parent_session_id=ROOT_SID,
            ),
            graph_instance_id=None,
        )
        await hook._notify_parent(ctx, session_id=CHILD_SID, content="late")
        assert await harness.bus.peek(ROOT_SID) == []
    finally:
        await harness.aclose()


async def test_approval_decision_not_starved_behind_held_traffic() -> None:
    """AWAITING_APPROVAL: held causal carriers must not starve the decision.

    The consume filter narrows an awaiting batch to decision prompts, so a
    decision is selected ahead of held traffic regardless of the drain batch
    limit (10) — the waiter settles instead of spinning forever.
    """
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.push_suspension("turn-1")
        harness.pipeline.push_finished("ack")  # the decision continuation
        harness.pipeline.push_finished("carrier-ack")
        for _ in range(12):
            harness.pipeline.push_finished("carrier-ack")
        wait_task = asyncio.create_task(
            harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation),
        )
        while True:
            record = _scope_metadata(await harness.registry.get(ROOT_SID))
            if record is not None and record.state is RequestScopeState.AWAITING_APPROVAL:
                break
            await asyncio.sleep(0.01)
        # Held causal traffic ahead of the decision in the SAME inbox.
        for _ in range(12):
            await harness.tree.deliver(ROOT_SID, _carrier(
                AgentMessageType.AGENT_RESULT, ROOT_SID, stamp=reservation.scope_id,
            ))
        decision = AgentMessageEnvelope(
            payload={"content": "", "approval_decision": ApprovalDecisionInput(
                tool_call_id="c1", action=ApprovalAction.ALLOW,
            ).model_dump(mode="json")},
            source=AgentAddress(name="user"),
            target=AgentAddress(name="main"),
            message_type=AgentMessageType.EXTERNAL_INPUT,
            session_id="conv1",
            agent_session_id=ROOT_SID,
        )
        await harness.tree.deliver(ROOT_SID, decision)

        result = await asyncio.wait_for(wait_task, timeout=10)
        assert result.outcome is RequestOutcome.FINISHED
        # The decision turn ran: the scripted pipeline saw the decision input.
        assert any(
            msg.approval_decision is not None for msg in harness.pipeline.seen
        ), "the decision continuation must be dispatched despite held traffic"
    finally:
        await harness.aclose()


async def test_awaiting_scope_consumes_only_decision_prompts() -> None:
    """The dispatch consume filter: EXTERNAL_INPUT only while AWAITING_APPROVAL
    (no hold/release churn over causal carriers), None otherwise."""
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.push_suspension("turn-1")
        waiter = asyncio.create_task(
            harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation),
        )
        while True:
            record = _scope_metadata(await harness.registry.get(ROOT_SID))
            if record is not None and record.state is RequestScopeState.AWAITING_APPROVAL:
                break
            await asyncio.sleep(0.01)
        assert await harness.tree.dispatch_consume_types(ROOT_SID) == frozenset(
            {AgentMessageType.EXTERNAL_INPUT.value},
        )
        await harness.tree.cancel_request(ROOT_SID, reservation.scope_id)
        assert (await asyncio.wait_for(waiter, timeout=5)).outcome is (
            RequestOutcome.CANCELLED
        )
        assert await harness.tree.dispatch_consume_types(ROOT_SID) is None
    finally:
        await harness.aclose()


async def test_hold_only_dispatch_suppresses_poller_wakeup() -> None:
    """A dispatch that held every envelope (no progress) must not wake the
    poller itself — held work retries on the interval tick or the next real
    signal, never as a consume/hold/release busy loop."""
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.push_suspension("turn-1")
        waiter = asyncio.create_task(
            harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation),
        )
        while True:
            record = _scope_metadata(await harness.registry.get(ROOT_SID))
            if record is not None and record.state is RequestScopeState.AWAITING_APPROVAL:
                break
            await asyncio.sleep(0.01)
        await harness.tree.deliver(ROOT_SID, _carrier(
            AgentMessageType.AGENT_RESULT, ROOT_SID, stamp=reservation.scope_id,
        ))
        # Stop the loop so the white-box drive below owns the wakeup event,
        # and cancel the waiter: a parked wait_quiesce deliberately kicks the
        # poller itself, which would mask the dispatch's own wakeup choice.
        await harness.pool.stop_poller()
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        harness.poller._wakeup_event.clear()
        harness.pipeline.push_finished("carrier-ack")
        harness.poller._maybe_start(ROOT_SID)
        while harness.poller._inflight:
            await asyncio.sleep(0.01)
        assert harness.poller._wakeup_event.is_set() is False, (
            "a hold-only dispatch round must not self-wake the poller"
        )
        await harness.tree.cancel_request(ROOT_SID, reservation.scope_id)
    finally:
        await harness.aclose()


async def test_note_scope_turn_ignores_facts_while_cancelling() -> None:
    """A turn fact racing an in-flight cancellation (a turn that suspends
    while the cancel finalizer drains) must not revive the scope out of
    CANCELLING — the finalizer owns settlement."""
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.gate = asyncio.Event()  # root turn in flight
        harness.pipeline.push_finished("done")
        wait_task = asyncio.create_task(
            harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation),
        )
        while not harness.pipeline.seen:
            await asyncio.sleep(0.01)

        finalizer_inside = asyncio.Event()
        release_finalizer = asyncio.Event()

        async def _gated_terminator(session_ids: Any) -> None:
            finalizer_inside.set()
            await release_finalizer.wait()

        harness.tree.attach_approval_terminator(_gated_terminator)
        cancel_task = asyncio.create_task(
            harness.tree.cancel_request(ROOT_SID, reservation.scope_id),
        )
        await asyncio.wait_for(finalizer_inside.wait(), timeout=5)
        record = _scope_metadata(await harness.registry.get(ROOT_SID))
        assert record is not None and record.state is RequestScopeState.CANCELLING

        # A SUSPENDED fact landing mid-cancel must be ignored.
        await harness.tree.note_scope_turn(
            ROOT_SID,
            RequestTurnFact(kind=RequestTurnFactKind.SUSPENDED, pending_approval_turn_uuid="t"),
        )
        record = _scope_metadata(await harness.registry.get(ROOT_SID))
        assert record is not None and record.state is RequestScopeState.CANCELLING

        release_finalizer.set()
        await asyncio.wait_for(cancel_task, timeout=5)
        assert (await asyncio.wait_for(wait_task, timeout=5)).outcome is (
            RequestOutcome.CANCELLED
        )
    finally:
        await harness.aclose()


async def test_settle_interrupted_scopes_terminates_pending_approvals() -> None:
    """Restart settlement reuses the SAME pool-owned approval terminator as
    cancellation: leftover suspended snapshots are audited and deleted, never
    left resumable behind an INTERRUPTED scope."""
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    reservation = await harness.pool.begin_request(ROOT_SID)
    await harness.tree.activate_reservation(reservation)
    await harness.pool.submit_input(ROOT_SID, harness.message("lost-prompt").model_copy(
        update={"metadata": {
            "message_id": "lost-prompt",
            REQUEST_SCOPE_ID_KEY: reservation.scope_id,
        }},
    ))
    fresh_poller = InboxPoller(harness.pool, interval=0.01, session_registry=harness.registry)
    fresh_tree = SessionTreeManager(
        tree_store=harness.tree._tree_store,
        node_store=harness.tree._node_store,
        track_store=harness.tree._track_store,
        bus=harness.bus,
        poller=fresh_poller,
        pool_name="main",
        workspace_root=".",
        session_registry=harness.registry,
    )
    fresh_poller.attach_tree_manager(fresh_tree)
    harness.pool.attach_poller(fresh_poller)
    harness.pool.tree = fresh_tree  # setter wires the pool terminator to the FRESH tree

    settled = await fresh_tree.settle_interrupted_scopes()
    assert settled == 1
    # The pool-owned terminator ran for the tree's sessions.
    assert harness.pipeline.terminated == 1
    record = _scope_metadata(await harness.registry.get(ROOT_SID))
    assert record is not None and record.state is RequestScopeState.INTERRUPTED
    await harness.aclose()


async def test_settle_interrupted_scopes_propagates_termination_failure() -> None:
    """A terminator failure during restart settlement reuses the cancel
    failure semantics: the scope settles FAILED with the real reason and the
    error propagates — the cleanup defect is never swallowed into a clean
    INTERRUPTED."""
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    reservation = await harness.pool.begin_request(ROOT_SID)
    await harness.tree.activate_reservation(reservation)
    await harness.pool.submit_input(ROOT_SID, harness.message("lost-prompt").model_copy(
        update={"metadata": {
            "message_id": "lost-prompt",
            REQUEST_SCOPE_ID_KEY: reservation.scope_id,
        }},
    ))
    fresh_poller = InboxPoller(harness.pool, interval=0.01, session_registry=harness.registry)
    fresh_tree = SessionTreeManager(
        tree_store=harness.tree._tree_store,
        node_store=harness.tree._node_store,
        track_store=harness.tree._track_store,
        bus=harness.bus,
        poller=fresh_poller,
        pool_name="main",
        workspace_root=".",
        session_registry=harness.registry,
    )

    async def _failing_terminator(session_ids: Any) -> None:
        raise RuntimeError("terminator exploded on restart")

    fresh_poller.attach_tree_manager(fresh_tree)
    harness.pool.attach_poller(fresh_poller)
    harness.pool.tree = fresh_tree
    # Attached AFTER the tree setter so the failing terminator is the one wired.
    fresh_tree.attach_approval_terminator(_failing_terminator)

    try:
        await fresh_tree.settle_interrupted_scopes()
    except RuntimeError as exc:
        assert "terminator exploded on restart" in str(exc)
    else:
        raise AssertionError("restart settlement must propagate termination failure")
    record = _scope_metadata(await harness.registry.get(ROOT_SID))
    assert record is not None
    assert record.state is RequestScopeState.FAILED
    assert record.outcome is RequestOutcome.FAILED
    assert record.fail_reason is not None and "terminator exploded" in record.fail_reason
    await harness.aclose()


async def test_cancel_terminates_approvals_for_non_resident_sessions() -> None:
    """Termination routes through a pool delegate, not a per-session resident
    lookup: a lazily-materialized child session that belongs to the tree but
    has NO registered instance in this pool still gets its approval batch
    terminated (the snapshot lives in the workspace turn store)."""
    harness = _Harness()
    await _register_scoped(harness)
    await harness.start()
    try:
        reservation = await harness.pool.begin_request(ROOT_SID)
        harness.pipeline.gate = asyncio.Event()  # root turn in flight
        harness.pipeline.push_finished("done")
        wait_task = asyncio.create_task(
            harness.pool.run_input(ROOT_SID, harness.message(), reservation=reservation),
        )
        while not harness.pipeline.seen:
            await asyncio.sleep(0.01)
        # A child session joins the tree; "helper" is NOT resident in the pool.
        await harness.tree.deliver("task1.helper", _carrier(
            AgentMessageType.TASK_REQUEST, "task1.helper", stamp=reservation.scope_id,
        ))
        await harness.tree.cancel_request(ROOT_SID, reservation.scope_id)
        assert (await asyncio.wait_for(wait_task, timeout=5)).outcome is (
            RequestOutcome.CANCELLED
        )
        # The delegate terminated BOTH tree sessions.
        assert harness.pipeline.terminated == 2
    finally:
        await harness.aclose()
