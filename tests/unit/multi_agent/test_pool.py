"""Tests for AgentPool dispatch behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.state import AgentState
from modex_graph.exceptions import GraphInterrupt


class _FakeBroker:
    async def consume(self, address):
        return None

    async def send_to(self, address, msg):
        pass


def _mock_tree(bus: object) -> SessionTreeManager:
    from modex_agent.multi_agent.envelope import AgentMessageEnvelope

    tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: AgentMessageEnvelope) -> None:
        await bus.send(sid, env)  # type: ignore[attr-defined]

    tree.deliver = _deliver
    return tree


@pytest.fixture
async def pool_with_bus():
    """An AgentPool wired to a real InMemoryInboxServer + LocalAgentMessageBus."""
    from modex_agent.multi_agent.bus import LocalAgentMessageBus
    from modex_agent.multi_agent.inbox.consumer import InboxConsumer
    from modex_agent.multi_agent.inbox.producer import InboxProducer
    from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer

    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer)
    p = AgentPool(
        broker=_FakeBroker(),
        agent_factory=MagicMock(),
        agent_bus=bus,
        inbox_consumer=consumer,
    )
    p.tree = _mock_tree(bus)
    yield p
    await p.shutdown_all(timeout=0.1)


class TestRunDispatch:
    """AgentPool._run_dispatch must propagate GraphInterrupt, not swallow it."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_run_dispatch_propagates_graph_interrupt(self, pool):
        """Regression: GraphInterrupt raised by the coroutine must propagate
        upward so the pipeline's approval handler can catch it.

        Before fix: caught by bare ``except Exception`` → logged as error
        and the agent state transitioned to ERROR.
        After fix: re-raised unchanged.
        """

        async def _raising_coro():
            raise GraphInterrupt(value=["test"])

        with pytest.raises(GraphInterrupt):
            await pool._run_dispatch("main", _raising_coro())

        # Agent should stay IDLE, not transition to ERROR
        assert pool.get_status("main") != AgentState.ERROR

    @pytest.mark.asyncio
    async def test_run_dispatch_does_not_transition_to_error_on_interrupt(self, pool):
        """If GraphInterrupt is swallowed, the agent transitions to ERROR.
        After the fix it must remain IDLE (or the state it had before)."""
        pool._status["main"] = AgentState.IDLE

        async def _raising_coro():
            raise GraphInterrupt(value=["test"])

        with pytest.raises(GraphInterrupt):
            await pool._run_dispatch("main", _raising_coro())

        assert pool._status.get("main") == AgentState.IDLE


class TestRegisterResidentTakesInstance:
    """ADR-0015 D3: register_resident takes a pre-built instance."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_register_resident_stores_prebuilt_instance(self, pool):
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.descriptor import AgentDescriptor

        descriptor = AgentDescriptor(address=AgentAddress(name="main"))
        fake_instance = MagicMock()
        fake_instance.stop = AsyncMock()
        await pool.register_resident(descriptor, fake_instance)
        assert pool.get("main") is fake_instance
        assert pool.get_status("main") == AgentState.IDLE
        # Verify instance was stored (no consumer task — _consumers dict is deleted)

    async def test_track_session_registers_correct_session_id(self):
        """Regression: _track_session called factory.create with
        external_id=session_id (a full '{prefix}.{agent}' string), causing
        encode_snowflake to double-encode the prefix and produce a different
        session_id — so two session records appeared for one subagent."""
        from unittest.mock import MagicMock

        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()
        registry = MagicMock()

        pool = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            session_factory=factory,
            session_registry=registry,
        )

        # The id that _create_dynamic_subagent already computed and put on the
        # envelope.  We want this EXACT session to be tracked — not a re-encoded
        # variant.
        existing = factory.create_with_prefix(
            agent_name="helper",
            prefix="abc123",
        )
        session_id = str(existing)

        pool._track_session(session_id, "helper", is_dynamic=True)

        # The fire-and-forget registration will be scheduled; we need to let
        # it run before checking.
        import asyncio

        await asyncio.sleep(0.05)

        assert registry.register.call_count == 1
        (registered_session,) = registry.register.call_args[0]
        assert str(registered_session) == session_id, (
            f"Expected {session_id!r}, got {str(registered_session)!r}"
        )


class TestShutdownOwnership:
    @staticmethod
    async def _register(pool: AgentPool, name: str, stop: AsyncMock) -> None:
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.descriptor import AgentDescriptor

        instance = MagicMock()
        instance.stop = stop
        await pool.register_resident(
            AgentDescriptor(address=AgentAddress(name=name)),
            instance,
        )

    @pytest.mark.asyncio
    async def test_timeout_owner_remains_registered_for_retry(self) -> None:
        # Given
        pool = AgentPool(broker=_FakeBroker(), agent_factory=MagicMock())
        timed_out = asyncio.Event()
        later_stopped = asyncio.Event()

        async def timeout_stop() -> None:
            timed_out.set()
            await asyncio.Event().wait()

        async def successful_stop() -> None:
            later_stopped.set()

        await self._register(pool, "timed-out", AsyncMock(side_effect=timeout_stop))
        await self._register(pool, "successful", AsyncMock(side_effect=successful_stop))

        # When
        completed = await pool.shutdown_all(timeout=0.01)

        # Then
        assert (
            completed,
            timed_out.is_set(),
            later_stopped.is_set(),
            pool.get("timed-out") is not None,
            pool.get_status("timed-out"),
            pool.get("successful") is None,
            pool.get_status("successful"),
        ) == (
            False,
            True,
            True,
            True,
            AgentState.SHUTTING_DOWN,
            True,
            AgentState.SHUTDOWN,
        )

    @pytest.mark.asyncio
    async def test_error_owner_remains_registered_and_does_not_block_later_stop(self) -> None:
        # Given
        pool = AgentPool(broker=_FakeBroker(), agent_factory=MagicMock())
        failed = asyncio.Event()
        later_stopped = asyncio.Event()

        async def error_stop() -> None:
            failed.set()
            raise RuntimeError("stop failed")

        async def successful_stop() -> None:
            later_stopped.set()

        await self._register(pool, "failed", AsyncMock(side_effect=error_stop))
        await self._register(pool, "successful", AsyncMock(side_effect=successful_stop))

        # When
        shutdown_error: RuntimeError | None = None
        completed = False
        try:
            completed = await pool.shutdown_all(timeout=0.1)
        except RuntimeError as exc:
            shutdown_error = exc

        # Then
        assert (
            completed,
            shutdown_error,
            failed.is_set(),
            later_stopped.is_set(),
            pool.get("failed") is not None,
            pool.get_status("failed"),
            pool.get("successful") is None,
            pool.get_status("successful"),
        ) == (
            False,
            None,
            True,
            True,
            True,
            AgentState.SHUTTING_DOWN,
            True,
            AgentState.SHUTDOWN,
        )

    @pytest.mark.asyncio
    async def test_cancellation_propagates_and_owner_remains_registered(self) -> None:
        # Given
        pool = AgentPool(broker=_FakeBroker(), agent_factory=MagicMock())
        stop_started = asyncio.Event()
        release_stop = asyncio.Event()
        stop_calls = 0

        async def cancelled_stop() -> None:
            nonlocal stop_calls
            stop_calls += 1
            stop_started.set()
            await release_stop.wait()

        await self._register(pool, "cancelled", AsyncMock(side_effect=cancelled_stop))

        # When
        shutdown = asyncio.create_task(pool.shutdown_all(timeout=1.0))
        await stop_started.wait()
        shutdown.cancel()

        # Then
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert pool.get("cancelled") is not None
        assert pool.get_status("cancelled") == AgentState.SHUTTING_DOWN

        release_stop.set()
        assert await pool.shutdown_all(timeout=1.0) is True
        assert stop_calls == 1
        assert pool.get("cancelled") is None

    @pytest.mark.asyncio
    async def test_incomplete_shutdown_can_be_retried_to_completion(self) -> None:
        # Given
        pool = AgentPool(broker=_FakeBroker(), agent_factory=MagicMock())
        stop = AsyncMock(side_effect=[RuntimeError("stop failed"), None])
        await self._register(pool, "retryable", stop)

        # When
        first_completed = await pool.shutdown_all(timeout=0.1)
        second_completed = await pool.shutdown_all(timeout=0.1)

        # Then
        assert first_completed is False
        assert second_completed is True
        assert stop.await_count == 2
        assert pool.get("retryable") is None

    @pytest.mark.asyncio
    async def test_cancelled_owner_shutdown_all_can_be_retried(self) -> None:
        # Given
        pool = AgentPool(broker=_FakeBroker(), agent_factory=MagicMock())
        stop = AsyncMock(side_effect=[asyncio.CancelledError(), None])
        await self._register(pool, "retryable", stop)

        # When
        with pytest.raises(asyncio.CancelledError):
            await pool.shutdown_all(timeout=0.1)
        completed = await pool.shutdown_all(timeout=0.1)

        # Then
        assert completed is True
        assert stop.await_count == 2
        assert pool.get("retryable") is None

    @pytest.mark.asyncio
    async def test_cancelled_idle_shutdown_can_be_retried(self) -> None:
        # Given
        pool = AgentPool(broker=_FakeBroker(), agent_factory=MagicMock())
        stop = AsyncMock(side_effect=[asyncio.CancelledError(), None])
        await self._register(pool, "retryable", stop)

        # When
        with pytest.raises(asyncio.CancelledError):
            await pool._shutdown_agent("retryable")
        await pool._shutdown_agent("retryable")

        # Then
        assert stop.await_count == 2
        assert pool.get("retryable") is None

    @pytest.mark.asyncio
    async def test_concurrent_shutdown_callers_share_owner_stop(self) -> None:
        # Given
        pool = AgentPool(broker=_FakeBroker(), agent_factory=MagicMock())
        stop_started = asyncio.Event()
        release_stop = asyncio.Event()
        second_caller_started = asyncio.Event()
        stop_calls = 0

        async def blocking_stop() -> None:
            nonlocal stop_calls
            stop_calls += 1
            stop_started.set()
            await release_stop.wait()

        await self._register(pool, "owner", AsyncMock(side_effect=blocking_stop))

        async def shutdown_from_second_caller() -> bool:
            second_caller_started.set()
            return await pool.shutdown_all(timeout=1.0)

        first_shutdown = asyncio.create_task(pool.shutdown_all(timeout=1.0))
        await stop_started.wait()
        second_shutdown = asyncio.create_task(shutdown_from_second_caller())
        await second_caller_started.wait()

        # When
        release_stop.set()
        results = await asyncio.gather(first_shutdown, second_shutdown)

        # Then
        assert results == [True, True]
        assert stop_calls == 1
        assert pool.get("owner") is None

    @pytest.mark.asyncio
    async def test_idle_shutdown_retains_owner_when_stop_fails(self) -> None:
        # Given
        pool = AgentPool(broker=_FakeBroker(), agent_factory=MagicMock())
        stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        await self._register(pool, "owner", stop)
        owner = pool.get("owner")

        # When
        await pool._shutdown_agent("owner")

        # Then
        assert stop.await_count == 1
        assert pool.get("owner") is owner
        assert pool.get_status("owner") == AgentState.SHUTTING_DOWN


class TestSubmitInputAndPollerHelpers:
    """Task 6: pool.submit_input (C2 payload) + poller helpers + dispatch_envelope."""

    @staticmethod
    def _build_input_message():
        from modex_agent.approval.types import ApprovalAction
        from modex_agent.approval.views import ApprovalDecisionInput
        from modex_agent.core.session_id import SessionIdFactory
        from modex_agent.core.types import InputMessage
        from modex_agent.media.models import Attachment, AttachmentLocator, Kind

        sess = SessionIdFactory().create(agent_name="main")
        attachment = Attachment(
            id="att-1",
            kind=Kind.IMAGE,
            name="cat.png",
            mime="image/png",
            size=1234,
            path="media/att-1",
            locator=AttachmentLocator.MEDIA,
        )
        decision = ApprovalDecisionInput(tool_call_id="call_abc", action=ApprovalAction.ALLOW)
        return InputMessage(
            content="hi",
            session=sess,
            sender_id="u1",
            chat_id="c1",
            channel="qq",
            source="qq",
            metadata={"k": "v"},
            approval_decision=decision,
            attachments_resolved=[attachment],
        ), str(sess)

    @pytest.mark.asyncio
    async def test_submit_input_preserves_payload_and_routes_to_inbox(self, pool_with_bus):
        """C2: submit_input must write a full BrokerInputPayload to external_input.

        approval_decision + attachments_resolved + sender_id + chat_id must survive
               the bus round-trip (webui approvals + ADR-0013 mechanism-B depend on it).
        """
        pool = pool_with_bus
        msg, sid = self._build_input_message()

        await pool.submit_input(sid, msg)

        assert sid in await pool._agent_bus.sessions_with_pending()
        envs = await pool._agent_bus.consume(sid, limit=10)
        assert envs, "expected one envelope"
        env = envs[0]
        assert env.message_type == "external_input"
        # C2 payload fields all preserved
        assert env.payload.get("approval_decision") is not None
        assert env.payload["approval_decision"]["tool_call_id"] == "call_abc"
        assert env.payload["approval_decision"]["action"] == "allow"
        assert env.payload["sender_id"] == "u1"
        assert env.payload["chat_id"] == "c1"
        atts = env.payload.get("attachments_resolved")
        assert atts and len(atts) == 1
        assert atts[0]["id"] == "att-1"
        assert env.payload["metadata"] == {"k": "v"}
        assert env.payload["content"] == "hi"

    @pytest.mark.asyncio
    async def test_submit_input_carries_session_routing(self, pool_with_bus):
        """Routing is carried by session_id/agent_session_id (target/source-kind
        are normalized by the bus producer/consumer pair — slimmed in Task 11)."""
        pool = pool_with_bus
        msg, sid = self._build_input_message()

        await pool.submit_input(sid, msg)

        envs = await pool._agent_bus.consume(sid, limit=10)
        env = envs[0]
        assert env.agent_session_id == sid
        # session_id prefix segment carries the conversation routing
        assert env.session_id == msg.session.session_id_prefix

    @pytest.mark.asyncio
    async def test_submit_input_round_trip_preserves_free_form_metadata(self, pool_with_bus):
        """Free-form InputMessage.metadata survives the broker round trip.

        ``submit_input`` serializes metadata into the C2 payload; the dispatch
        side must parse it back so per-turn keys (e.g. ``trace_id``) reach
        ``TurnContextBuilder`` instead of being dropped in transit.
        """
        pool = pool_with_bus
        msg, sid = self._build_input_message()
        msg = msg.model_copy(update={"metadata": {"trace_id": "trace-abc", "k": "v"}})

        await pool.submit_input(sid, msg)

        envs = await pool._agent_bus.consume(sid, limit=10)
        input_msg = envs[0].to_input_message(session=msg.session)
        assert input_msg.metadata["trace_id"] == "trace-abc"
        assert input_msg.metadata["k"] == "v"
        assert input_msg.metadata["agent_session_id"] == sid

    @pytest.mark.asyncio
    async def test_sessions_with_pending_lists_pool_sessions(self, pool_with_bus):
        pool = pool_with_bus
        msg, sid = self._build_input_message()
        assert await pool.sessions_with_pending() == []

        await pool.submit_input(sid, msg)

        pending = await pool.sessions_with_pending()
        assert sid in pending

    @pytest.mark.asyncio
    async def test_consume_inbox_only_types_filters(self, pool_with_bus):
        """consume_inbox(only_types=...) filters — external stays pending."""
        pool = pool_with_bus
        msg, sid = self._build_input_message()
        await pool.submit_input(sid, msg)
        # Seed a task_request envelope on the same session
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.envelope import AgentMessageEnvelope

        await pool.tree.deliver(
            sid,
            AgentMessageEnvelope(
                payload={"content": "do task", "message_type": "task_request"},
                source=AgentAddress(kind="agent", name="boss"),
                target=AgentAddress(kind="agent", name="main"),
                message_type="task_request",
                session_id=sid,
                agent_session_id=sid,
            ),
        )

        agent_only = await pool.consume_inbox(sid, only_types={"task_request"})
        assert agent_only, "expected the task_request to be returned"
        assert all(e.message_type == "task_request" for e in agent_only)

        # The external_input must still be pending (only_types filtered it out)
        remaining = await pool.consume_inbox(sid)
        assert any(e.message_type == "external_input" for e in remaining)

    @pytest.mark.asyncio
    async def test_dispatch_envelope_is_public(self):
        """dispatch_envelope is the renamed public _run_inbox_turn (C4/C5)."""
        assert hasattr(AgentPool, "dispatch_envelope")
        assert not hasattr(AgentPool, "_run_inbox_turn")
