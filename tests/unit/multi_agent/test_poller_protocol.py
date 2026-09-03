"""InboxPoller protocol tests — the poller is the sole between-turn driver.

Mirrors the invariants the old Drainer protocol tests asserted, but on the
poll-driven model (one InboxPoller per pool + unified inbox + non-blocking
consume + dispatch_envelope):

- single-flight (busy session skipped — fold-in hook handles mid-turn)
- drain-to-empty (N messages → poller consumes batch → one turn per envelope)
- external_input starts a turn
- lazy materialize on first turn
- materialize-failure leaves the message in the inbox
- no-drop under concurrent sends

Per-pool isolation is now STRUCTURAL (each pool owns its bus + poller), so
the old shared-bus signal-routing test is moot here — Task 13 covers the
real multi-pool isolation test.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.multi_agent import AgentPool, DefaultAgentFactory
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.state import AgentState
from modex_agent.scope.spec import AgentSpec

# ── Test helpers ──────────────────────────────────────────────────────────


class _FakeBroker:
    async def consume(self, address):
        return None

    async def send_to(self, address, msg):
        pass


class _MockAgentFactory(DefaultAgentFactory):
    async def create_agent(self, descriptor, **kwargs):
        pipeline = MagicMock()
        pipeline.process_message = AsyncMock()
        pipeline.hook_runner = None
        pipeline.hooks = []
        pipeline.stop = AsyncMock()
        pipeline.emitter_factory = None
        pipeline.workspace_manager = None
        pipeline.interceptor_chain = None
        pipeline.governance = None
        pipeline.skill_resolver = None
        pipeline.command_processor = None
        pipeline.runtime_services = None
        return AgentInstance(
            descriptor=descriptor,
            context_manager=MagicMock(),
            pipeline=pipeline,
        )


async def _make_poller_pool(interval: float = 0.02):
    """An AgentPool wired with a real bus + InboxPoller, with a resident 'main'.

    The poller is attached and started; each test awaits long enough for ≥1
    tick, then stops the poller.
    """
    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer)
    pool = AgentPool(
        broker=_FakeBroker(),
        agent_factory=_MockAgentFactory(),
        agent_bus=bus,
        inbox_consumer=consumer,
        session_factory=SessionIdFactory(),
    )

    _tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

    async def _tree_deliver(sid: str, env: AgentMessageEnvelope) -> None:
        await bus.send(sid, env)

    _tree.deliver = _tree_deliver
    pool.tree = _tree

    descriptor = AgentDescriptor(address=AgentAddress(name="main"))
    instance = await pool._agent_factory.create_agent(descriptor, broker=_FakeBroker())
    pool._agents["main"] = instance
    pool._status["main"] = AgentState.IDLE

    poller = InboxPoller(pool, interval=interval)
    pool.attach_poller(poller)
    pool.start_poller()
    return pool, bus, poller


def _envelope(content: str = "test", session_id: str = "pfx.main") -> AgentMessageEnvelope:
    return AgentMessageEnvelope(
        payload={"content": content, "message_type": "agent_message"},
        source=AgentAddress(name="src"),
        target=AgentAddress(name="main"),
        message_type="agent_message",
        session_id="pfx.main",
        agent_session_id=session_id,
    )


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poller_single_message_runs_one_turn():
    pool, bus, poller = await _make_poller_pool()
    try:
        main = pool._agents["main"]
        await bus.send("pfx.main", _envelope("hello"))
        await asyncio.sleep(0.1)
        assert main.pipeline.process_message.await_count == 1
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poller_drains_batch_one_turn_per_envelope():
    """Three messages → poller consumes the batch in one sweep; one
    process_message call per envelope; inbox drained to empty."""
    pool, bus, poller = await _make_poller_pool()
    try:
        main = pool._agents["main"]
        for i in range(3):
            await bus.send("pfx.main", _envelope(f"m{i}"))
        await asyncio.sleep(0.15)
        assert main.pipeline.process_message.await_count == 3
        assert "pfx.main" not in await bus.sessions_with_pending()
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poller_single_flight_busy_session_skipped():
    """While a turn is in-flight for a session, subsequent ticks must NOT
    start a second concurrent turn (single-flight). The fold-in hook handles
    mid-turn messages."""
    pool, bus, poller = await _make_poller_pool()
    try:
        main = pool._agents["main"]
        started: list[int] = []

        async def _slow(_msg):
            started.append(1)
            await asyncio.sleep(0.5)

        main.pipeline.process_message = _slow

        await bus.send("pfx.main", _envelope("first"))
        await asyncio.sleep(0.15)  # poller starts the slow turn
        # Send more messages while the turn is busy; ticks must skip.
        await bus.send("pfx.main", _envelope("second"))
        await bus.send("pfx.main", _envelope("third"))
        await asyncio.sleep(0.15)
        assert len(started) == 1  # only one concurrent turn
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poller_external_input_runs_a_turn():
    """submit_input writes an external_input envelope; the poller starts a
    turn for it."""
    pool, bus, poller = await _make_poller_pool()
    try:
        main = pool._agents["main"]
        msg = InputMessage(
            content="external hello",
            session=SessionInfo(session_id="pfx.main", agent_name="main"),
        )
        await pool.submit_input("pfx.main", msg)
        await asyncio.sleep(0.15)
        assert main.pipeline.process_message.await_count == 1
        call_args = main.pipeline.process_message.call_args
        assert call_args is not None
        assert call_args[0][0].content == "external hello"
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poller_no_drop_under_concurrent_sends():
    pool, bus, poller = await _make_poller_pool()
    try:
        main = pool._agents["main"]
        await asyncio.gather(*[bus.send("pfx.main", _envelope(f"c{i}")) for i in range(5)])
        await asyncio.sleep(0.2)
        assert "pfx.main" not in await bus.sessions_with_pending()
        assert main.pipeline.process_message.await_count >= 1
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_dispatch_lifecycle_fires_start_and_end() -> None:
    pool, _bus, poller = await _make_poller_pool()
    await poller.stop()
    lifecycle: list[str] = []

    async def on_dispatch_start(_sid: str) -> None:
        lifecycle.append("start")

    async def on_dispatch_end(sid: str) -> None:
        assert sid in poller._inflight
        lifecycle.append("end")

    tree_manager = MagicMock()
    tree_manager.on_dispatch_start = AsyncMock(side_effect=on_dispatch_start)
    tree_manager.on_dispatch_end = AsyncMock(side_effect=on_dispatch_end)
    poller.attach_tree_manager(tree_manager)
    poller._inflight["pfx.main"] = MagicMock(done=lambda: False)

    await poller._run_turn("pfx.main", pool._agents["main"])

    assert lifecycle == ["start", "end"]


@pytest.mark.asyncio
async def test_dispatch_end_exception_doesnt_crash_poller() -> None:
    pool, _bus, poller = await _make_poller_pool()
    await poller.stop()

    async def fail_dispatch_end(sid: str) -> None:
        assert sid in poller._inflight
        raise RuntimeError("tree store unavailable")

    tree_manager = MagicMock()
    tree_manager.on_dispatch_start = AsyncMock()
    tree_manager.on_dispatch_end = AsyncMock(side_effect=fail_dispatch_end)
    poller.attach_tree_manager(tree_manager)
    poller._inflight["pfx.main"] = MagicMock(done=lambda: False)
    poller.signal_wakeup = MagicMock()

    await poller._run_turn("pfx.main", pool._agents["main"])

    assert "pfx.main" not in poller._inflight
    tree_manager.on_dispatch_end.assert_awaited_once_with("pfx.main")
    poller.signal_wakeup.assert_called_once_with()


# ── Lazy materialize on first turn ────────────────────────────────────────


@pytest.mark.asyncio
async def test_poller_lazy_materializes_missing_subagent():
    """A provocation for a session with no live instance triggers
    template.materialize on the first turn (ADR-0015 D3)."""
    from modex_agent.core.session_registry import InMemorySessionRegistry
    from modex_agent.multi_agent.template import AgentTemplate

    pool, bus, poller = await _make_poller_pool()

    materialized = {"called": False}
    captured_parent: dict[str, object] = {}

    class _FakeTemplate(AgentTemplate):
        async def materialize(self, parent_session, invocation_id, deps):
            materialized["called"] = True
            captured_parent["parent_session"] = parent_session
            inst = MagicMock()
            inst.pipeline = MagicMock()
            inst.pipeline.process_message = AsyncMock()
            pool._agents["scout"] = inst
            return inst

    pool.template_registry = MagicMock()
    pool.template_registry.get_template = MagicMock(
        return_value=_FakeTemplate(spec=AgentSpec(name="scout"))
    )
    pool.materialize_deps = MagicMock()
    pool.pool_name = "main"

    sf = SessionIdFactory()
    child_session = sf.create_with_prefix(
        agent_name="scout",
        prefix="inv1",
        parent_session_id=sf.create(agent_name="main"),
    )
    pool._session_registry = InMemorySessionRegistry()
    await pool._session_registry.register(child_session)

    try:
        await bus.send("inv1.scout", _envelope("hello", session_id="inv1.scout"))
        await asyncio.sleep(0.2)
        assert materialized["called"] is True
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poller_materialize_failure_leaves_message_in_inbox():
    """On materialize failure the message stays in the inbox (no silent drop)."""
    from modex_agent.multi_agent.template import AgentTemplate

    pool, bus, poller = await _make_poller_pool()

    class _FailingTemplate(AgentTemplate):
        async def materialize(self, parent_session, invocation_id, deps):
            raise RuntimeError("MCP server hung")

    pool.template_registry = MagicMock()
    pool.template_registry.get_template = MagicMock(
        return_value=_FailingTemplate(spec=AgentSpec(name="scout"))
    )
    pool.materialize_deps = MagicMock()
    pool.pool_name = "main"

    try:
        await bus.send("inv2.scout", _envelope("hello", session_id="inv2.scout"))
        await asyncio.sleep(0.2)
        # Message must NOT be lost from the inbox.
        assert "inv2.scout" in await bus.sessions_with_pending()
    finally:
        await poller.stop()


# ── Convergence: parent link travels in the message ───────────────────────


@pytest.mark.asyncio
async def test_dispatch_stamps_parent_from_envelope_without_registry():
    """Convergence proof: ``ctx.session.parent_session_id`` comes from the
    envelope, NOT recovered from a session store. A pool wired with NO session
    registry and NO session store still yields a turn session carrying the
    envelope's parent link — so subagent messaging is independent of which
    workspace is active (workspace home-vs-not can no longer drop the parent).

    Must fail on the pre-convergence code, which read the parent from
    ``_resolve_session_info`` (registry/store) and got ``None`` here.
    """
    from modex_agent.multi_agent.message_type import AgentMessageType

    pool, _bus, poller = await _make_poller_pool()  # no session_registry / no store
    try:
        captured: dict[str, object] = {}

        inst = MagicMock()
        inst.pipeline = MagicMock()

        async def _capture(msg):
            captured["session"] = msg.session

        inst.pipeline.process_message = _capture
        pool._agents["scout"] = inst
        pool._status["scout"] = AgentState.IDLE

        envelope = AgentMessageEnvelope(
            payload={"content": "hi", "message_type": AgentMessageType.TASK_REQUEST},
            source=AgentAddress(name="main"),
            target=AgentAddress(name="scout"),
            message_type=AgentMessageType.TASK_REQUEST,
            session_id="conv.main",
            agent_session_id="inv1.scout",
            parent_session_id="conv.main",
            invocation_id="inv1",
        )
        await pool.dispatch_envelope("inv1.scout", inst, envelope)

        session: SessionInfo | None = captured.get("session")  # type: ignore[assignment]
        assert session is not None, "process_message was not invoked"
        assert session.parent_session_id == "conv.main"
        assert session.session_id == "inv1.scout"
    finally:
        await poller.stop()
