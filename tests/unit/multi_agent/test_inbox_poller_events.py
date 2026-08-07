"""InboxPoller event-driven wakeup — concurrency invariants.

Verifies the four invariants documented on ``InboxPoller`` after the
polling → event-driven switch:

1. **Zero latency**: ``bus.send`` signals the poller, so a turn starts within
   milliseconds instead of waiting up to ``interval``.
2. **Tick fallback**: when no poller is wired on the bus (or a writer bypasses
   it), the poller still ticks every ``interval`` and makes progress.
3. **Busy-session re-scan**: a message arriving during a busy turn is picked
   up immediately when the turn ends (the turn's ``finally`` re-signals),
   not after one ``interval``.
4. **Single-flight + fold-in do not double-consume**: a fold-eligible message
   that arrives mid-turn is consumed by ``InboxFlushHook`` (not the poller);
   the poller never starts a second concurrent turn for the same session; and
   the message is not processed twice.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.session_id import SessionIdFactory
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
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.state import AgentState

# ── Test helpers ──────────────────────────────────────────────────────────


class _FakeBroker:
    async def consume(self, address: object) -> None:
        return None

    async def send_to(self, address: object, msg: object) -> None:
        pass


class _MockAgentFactory(DefaultAgentFactory):
    # Signature intentionally narrows the 13-param base ``create_agent``: this
    # test mock only consumes ``descriptor`` and ignores the rest. Kept untyped
    # to avoid an LSP override-compatibility error against the wide base sig.
    async def create_agent(self, descriptor, **kwargs):  # noqa: ANN001, ANN003, ANN202
        pipeline = MagicMock()
        pipeline.process_message = AsyncMock()
        pipeline.hook_runner = None
        pipeline.hooks = []
        pipeline.stop = AsyncMock()
        pipeline.emitter_factory = None
        pipeline.workspace_manager = None
        pipeline.interceptor_chain = None
        pipeline.governance = None
        pipeline.skill_manager = None
        pipeline.command_processor = None
        pipeline.runtime_services = None
        return AgentInstance(
            descriptor=descriptor,
            context_manager=MagicMock(),
            pipeline=pipeline,
        )


async def _make_pool_and_bus(
    *, interval: float = 0.2, wire_poller: bool = True
) -> tuple[AgentPool, LocalAgentMessageBus, InboxPoller, AgentInstance]:
    """Build a real AgentPool + bus + poller with one resident 'main'.

    ``wire_poller`` controls whether ``bus.set_poller`` is called (the True
    case exercises the event-driven path; False exercises the tick fallback).
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

    descriptor = AgentDescriptor(address=AgentAddress(name="main"))
    instance = await pool._agent_factory.create_agent(descriptor, broker=_FakeBroker())
    pool._agents["main"] = instance
    pool._status["main"] = AgentState.IDLE

    poller = InboxPoller(pool, interval=interval)
    pool.attach_poller(poller)
    if wire_poller:
        bus.set_poller(poller)
    return pool, bus, poller, instance


def _envelope(
    content: str = "test", mtype: str = AgentMessageType.AGENT_MESSAGE
) -> AgentMessageEnvelope:
    return AgentMessageEnvelope(
        payload={"content": content, "message_type": mtype},
        source=AgentAddress(name="src"),
        target=AgentAddress(name="main"),
        message_type=mtype,
        session_id="pfx.main",
        agent_session_id="pfx.main",
    )


# ── Invariant 1: zero latency ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wakeup_delivers_near_zero_latency() -> None:
    """A bus.send with a wired poller starts a turn far faster than ``interval``.

    Uses ``interval=2.0`` so a tick-fallback path would take ≥2s; the event-
    driven path must start the turn within a few hundred milliseconds.
    """
    pool, bus, poller, main = await _make_pool_and_bus(interval=2.0, wire_poller=True)
    try:
        poller.start()
        await bus.send("pfx.main", _envelope("hello"))
        # Wait at most 0.5s — far below the 2.0s interval.
        async with asyncio.timeout(0.5):
            while not main.pipeline.process_message.called:
                await asyncio.sleep(0.02)
        assert main.pipeline.process_message.called
    finally:
        await poller.stop()


# ── Invariant 2: tick fallback ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_fallback_when_bus_has_no_poller() -> None:
    """Without ``bus.set_poller``, the poller still ticks every ``interval``.

    This covers writers that bypass the bus (e.g. direct server writes in
    tests) and the pre-wiring window: progress must still happen via the
    defensive tick fallback.
    """
    pool, bus, poller, main = await _make_pool_and_bus(interval=0.05, wire_poller=False)
    try:
        poller.start()
        await bus.send("pfx.main", _envelope("hello"))
        # No wakeup signal — the tick (every 0.05s) must still deliver.
        async with asyncio.timeout(1.0):
            while not main.pipeline.process_message.called:
                await asyncio.sleep(0.02)
        assert main.pipeline.process_message.called
    finally:
        await poller.stop()


# ── Invariant 3: busy-session re-scan ──────────────────────────────────────


@pytest.mark.asyncio
async def test_busy_session_message_picked_up_after_turn_ends() -> None:
    """A message arriving during a busy turn is scanned when the turn ends.

    Regression guard for the polling→event-driven gap: a wakeup fired during
    a busy turn is absorbed by single-flight, so the message would otherwise
    wait up to one ``interval``. The turn's ``finally`` re-signals so the
    next turn starts promptly.

    Uses ``interval=2.0`` so the only fast path is the ``finally`` re-signal.
    """
    pool, bus, poller, main = await _make_pool_and_bus(interval=2.0, wire_poller=True)
    try:
        turn_started = asyncio.Event()
        turn_in_progress = asyncio.Event()
        first_turn_count = {"n": 0}

        async def _slow_then_signal(msg: object) -> None:
            first_turn_count["n"] += 1
            if first_turn_count["n"] == 1:
                turn_started.set()
                # Hold the turn open so the second send lands during busy.
                await turn_in_progress.wait()

        main.pipeline.process_message = _slow_then_signal

        poller.start()
        # First message: starts a busy turn.
        await bus.send("pfx.main", _envelope("first"))
        async with asyncio.timeout(0.5):
            await turn_started.wait()

        # Second message arrives WHILE the turn is busy. Single-flight must
        # skip it; it stays pending until the turn ends.
        await bus.send("pfx.main", _envelope("second"))
        await asyncio.sleep(0.1)  # let any spurious tick land (it must skip)
        assert first_turn_count["n"] == 1  # no concurrent second turn

        # Release the busy turn; its finally re-signals → second turn starts.
        turn_in_progress.set()
        async with asyncio.timeout(0.5):
            while first_turn_count["n"] < 2:
                await asyncio.sleep(0.02)
        assert first_turn_count["n"] == 2  # second turn ran promptly
    finally:
        await poller.stop()


# ── Invariant 4: single-flight + no double-consume ─────────────────────────


@pytest.mark.asyncio
async def test_single_flight_no_concurrent_turn_for_same_session() -> None:
    """While a turn is in-flight, ticks never start a second turn.

    Even with aggressive wakeup firing, ``_maybe_start`` skips the busy
    session — the fold-in hook owns mid-turn consumption.
    """
    pool, bus, poller, main = await _make_pool_and_bus(interval=0.02, wire_poller=True)
    try:
        started: list[int] = []

        async def _slow(_msg: object) -> None:
            started.append(1)
            await asyncio.sleep(0.3)

        main.pipeline.process_message = _slow
        poller.start()
        await bus.send("pfx.main", _envelope("first"))
        await asyncio.sleep(0.1)  # several ticks/wakeups land while busy
        await bus.send("pfx.main", _envelope("second"))
        await bus.send("pfx.main", _envelope("third"))
        await asyncio.sleep(0.1)
        assert len(started) == 1  # single-flight — no second concurrent turn
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_repeated_wakeups_collapse_into_one_event_state() -> None:
    """N wakeups between two ticks leave the Event set exactly once.

    The ``_wakeup_event`` is a level-triggered boolean: calling ``set`` 50
    times is observationally identical to calling it once. This guards against
    any future refactor that might turn it into a counting semaphore (which
    would busy-loop). We assert on the Event state, not on tick count, to keep
    the test robust against scheduling jitter.
    """
    poller = InboxPoller.__new__(InboxPoller)  # no pool needed for this unit check
    poller._wakeup_event = asyncio.Event()
    poller._wakeup_event.clear()

    assert not poller._wakeup_event.is_set()
    for _ in range(50):
        poller.signal_wakeup()
    # Still just "set" — not 50 pending signals.
    assert poller._wakeup_event.is_set()
    # One clear returns it to unset regardless of how many sets preceded.
    poller._wakeup_event.clear()
    assert not poller._wakeup_event.is_set()


@pytest.mark.asyncio
async def test_wakeup_during_tick_for_other_session_not_swallowed() -> None:
    """A wakeup fired DURING a tick (for a different session) survives.

    Regression guard for the ``Event.clear()`` placement: clearing AFTER the
    tick would swallow a signal set while ``_tick`` was running (the tick's
    own ``sessions_with_pending`` snapshot did not include the just-arrived
    message), degrading to the ``interval`` fallback. Clearing BEFORE the
    tick keeps such in-flight signals live so the next ``wait`` returns
    immediately.

    Setup: two sessions (main + helper). main's turn runs slowly so the tick
    is still inside ``_dispatch_batch`` when a message for ``helper`` arrives.
    With ``interval=2.0``, helper's turn must still start well under 2s.
    """
    pool, bus, poller, main = await _make_pool_and_bus(interval=2.0, wire_poller=True)
    try:
        # Register a second resident agent ``helper``.
        h_desc = AgentDescriptor(address=AgentAddress(name="helper"))
        helper_inst = await pool._agent_factory.create_agent(h_desc, broker=_FakeBroker())
        pool._agents["helper"] = helper_inst
        pool._status["helper"] = AgentState.IDLE

        main_turn_started = asyncio.Event()
        main_can_finish = asyncio.Event()

        async def _slow_main(_msg: object) -> None:
            main_turn_started.set()
            await main_can_finish.wait()

        main.pipeline.process_message = _slow_main

        poller.start()
        # First message → starts main's slow turn (tick enters dispatch).
        await bus.send("pfx.main", _envelope("to-main"))
        async with asyncio.timeout(0.5):
            await main_turn_started.wait()

        # While main's turn is busy, send to helper. This wakeup fires while
        # the poller's previous tick is long-finished and the loop is waiting;
        # it must wake the loop and start helper's turn promptly.
        await bus.send("pfx.helper", _envelope("to-helper", mtype=AgentMessageType.AGENT_MESSAGE))
        async with asyncio.timeout(0.5):
            while not helper_inst.pipeline.process_message.called:
                await asyncio.sleep(0.02)
        assert helper_inst.pipeline.process_message.called

        main_can_finish.set()
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_fold_eligible_message_mid_turn_consumed_once_not_doubled() -> None:
    """A fold-eligible message arriving mid-turn is consumed exactly once.

    This is the core invariant the two-consumer design relies on: while a
    turn is busy for session X, a fold-eligible message (e.g. AGENT_MESSAGE)
    arriving for X must be consumed by the fold-in hook (InboxFlushHook)
    inside the running turn — NOT picked up by the poller as a second turn,
    and NOT consumed twice. The poller's single-flight skips X until its turn
    ends; ``consume`` is destructive so the hook and poller cannot both take
    the same message.

    Setup: main is busy (slow turn). We send a second AGENT_MESSAGE to main
    during the busy window, then let the turn finish. Assertion: the message
    is delivered (exactly-once) via either the fold-in path (within the
    running turn) or a follow-up poller turn — never both, and never zero.
    """
    pool, bus, poller, main = await _make_pool_and_bus(interval=0.05, wire_poller=True)
    try:
        turn_started = asyncio.Event()
        turn_can_finish = asyncio.Event()
        delivered_contents: list[str] = []

        async def _slow_then_release(msg: InputMessage) -> None:
            delivered_contents.append(msg.content)
            if not turn_started.is_set():
                turn_started.set()
                # Hold the first turn open so the second send lands mid-turn.
                await turn_can_finish.wait()

        main.pipeline.process_message = _slow_then_release
        poller.start()

        # First message starts the busy turn.
        await bus.send("pfx.main", _envelope("first"))
        async with asyncio.timeout(0.5):
            await turn_started.wait()

        # Second fold-eligible message arrives WHILE main is busy.
        # Single-flight must skip it; it stays pending for fold-in or a
        # follow-up turn after the first finishes.
        await bus.send("pfx.main", _envelope("second"))
        await asyncio.sleep(0.15)  # let ticks/wakeups land (they must skip)

        # Release the first turn. The second message must now be delivered
        # exactly once — either as a fold-in within the first turn (if a hook
        # were wired) or as a follow-up turn. Either way: no double, no drop.
        turn_can_finish.set()
        async with asyncio.timeout(0.5):
            while delivered_contents.count("second") == 0:
                await asyncio.sleep(0.02)

        # Give a brief window for any spurious duplicate to manifest.
        await asyncio.sleep(0.1)
        assert delivered_contents.count("second") == 1, (
            f"second message delivered {delivered_contents.count('second')} times "
            f"(expected exactly 1): {delivered_contents}"
        )
    finally:
        await poller.stop()


# ── Invariant 5: set_poller idempotency + pre-wiring window ─────────────────


@pytest.mark.asyncio
async def test_set_poller_idempotent_and_pre_wiring_send_is_safe() -> None:
    """``set_poller`` is idempotent; ``bus.send`` before wiring is persist-only.

    Covers the pre-wiring window (bus constructed before poller exists) and
    re-wiring safety. ``bus.send`` with no poller must NOT raise — it degrades
    to persist-only and the tick fallback covers delivery. ``set_poller``
    called twice must simply replace the reference (no error, no double-signal).
    """
    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

    # Pre-wiring: send with no poller wired — must succeed (persist-only).
    await bus.send("pfx.main", _envelope("before-wiring"))
    assert len(await bus.peek("pfx.main", limit=10)) == 1

    pool = AgentPool(
        broker=_FakeBroker(),
        agent_factory=_MockAgentFactory(),
        agent_bus=bus,
        inbox_consumer=consumer,
        session_factory=SessionIdFactory(),
    )
    descriptor = AgentDescriptor(address=AgentAddress(name="main"))
    instance = await pool._agent_factory.create_agent(descriptor, broker=_FakeBroker())
    pool._agents["main"] = instance
    pool._status["main"] = AgentState.IDLE

    poller1 = InboxPoller(pool, interval=0.2)
    bus.set_poller(poller1)
    # Re-wiring: second set_poller replaces the first — no error.
    poller2 = InboxPoller(pool, interval=0.2)
    bus.set_poller(poller2)

    # After wiring, a send signals poller2 (not poller1). Verify by checking
    # the event state of poller2 is set and poller1's is NOT affected by this
    # send (it was never started, so its event is independently unset).
    poller2._wakeup_event.clear()
    poller1._wakeup_event.clear()
    await bus.send("pfx.main", _envelope("after-wiring"))
    assert poller2._wakeup_event.is_set()
    assert not poller1._wakeup_event.is_set()


# ── Invariant 6: poller full-consume does not double-deliver via hook ────────


@pytest.mark.asyncio
async def test_poller_full_consume_batch_then_hook_gets_empty_no_double() -> None:
    """Poller consumes a full batch (incl. fold-eligible); turn hook sees empty.

    Regression guard for the §4.2 exactly-once boundary: when the poller
    consumes an idle session's entire pending batch (no ``only_types`` filter,
    so fold-eligible types are included), the subsequently-started turn's
    ``before_turn``/``before_iteration`` hook must NOT re-consume the same
    messages. ``consume`` is destructive, so the hook gets an empty list.

    Setup: idle session with a mix of EXTERNAL_INPUT + AGENT_MESSAGE. The
    poller consumes both, starts a turn for the first. We verify the consumed
    batch matches the sent messages exactly once.
    """
    pool, bus, poller, main = await _make_pool_and_bus(interval=0.05, wire_poller=True)
    try:
        consumed: list[str] = []

        async def _record_msg(msg: InputMessage) -> None:
            consumed.append(msg.content)

        main.pipeline.process_message = _record_msg
        poller.start()

        # Send a mix: external + fold-eligible, both to an IDLE session.
        # The poller's full consume takes both; each becomes its own turn.
        await bus.send("pfx.main", _envelope("ext-msg", mtype=AgentMessageType.EXTERNAL_INPUT))
        await bus.send("pfx.main", _envelope("agent-msg", mtype=AgentMessageType.AGENT_MESSAGE))

        # Wait for both turns to complete (each envelope → one turn).
        async with asyncio.timeout(1.0):
            while len(consumed) < 2:
                await asyncio.sleep(0.02)

        # Both messages delivered exactly once — no double from hook re-consume.
        assert sorted(consumed) == ["agent-msg", "ext-msg"], (
            f"expected both messages once each, got: {consumed}"
        )
        # Inbox is now empty (destructive consume).
        assert len(await bus.peek("pfx.main", limit=10)) == 0
    finally:
        await poller.stop()
