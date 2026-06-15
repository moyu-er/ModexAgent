"""Tests for AgentPool dispatch behavior."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.graph.interrupt import GraphInterrupt
from framework.multi_agent.pool import AgentPool
from framework.multi_agent.state import AgentState


class _FakeBroker:
    async def consume(self, address):
        return None

    async def send_to(self, address, msg):
        pass


class TestRunDispatch:
    """AgentPool._run_dispatch must propagate GraphInterrupt, not swallow it."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
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


class TestDispatchTaskRequestFallback:
    """_dispatch_task_request must accept legacy envelopes with ``content`` as
    a defensive fallback for ``task_prompt``."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_dispatch_falls_back_to_content_when_task_prompt_missing(self, pool):
        """When the envelope payload has ``content`` but no ``task_prompt``,
        _dispatch_task_request should still extract the task via the fallback."""
        from framework.core.types import InputMessage
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.descriptor import AgentDescriptor, AgentInstance
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.pipeline.pipeline import AgentPipeline

        from framework.core.agent import Agent
        from framework.core.tool_manager import InMemoryToolManager

        desc = AgentDescriptor(address=AgentAddress(name="worker"))
        agent_stub = MagicMock(spec=Agent)
        agent_stub.name = "worker"

        pipeline_stub = MagicMock(spec=AgentPipeline)
        processed_content = []

        async def _fake_process(msg):
            processed_content.append(msg.content)
            from framework.core.emitter import AgentResult
            return AgentResult(content="done")

        pipeline_stub.process_message.side_effect = _fake_process
        instance = AgentInstance(
            descriptor=desc,
            pipeline=pipeline_stub,
            context_manager=MagicMock(),
        )

        envelope = AgentMessageEnvelope(
            payload={"content": "legacy task", "message_type": "task_request"},
            source=AgentAddress(name="main"),
            message_type="task_request",
            conversation_id="conv",
        )

        await pool._dispatch_task_request(instance, desc, envelope)
        assert processed_content, "Pipeline should have been called"
        assert processed_content[0] == "legacy task", (
            f"Expected 'legacy task' but got {processed_content[0]!r}"
        )


class TestPoolSessionLockSerialization:
    """Pool per-session lock must serialize same-session dispatch, preventing overlap."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_same_session_dispatches_are_serialized(self, pool):
        """Two concurrent dispatches on the same session must not overlap.
        The pool get_lock acquires per-session, serializing pipeline execution."""
        sid = "conv:worker:task-X"
        lock = pool.get_lock(sid)

        enter_order: list[int] = []
        exit_order: list[int] = []

        async def _dispatch_with_index(idx: int):
            enter_order.append(idx)
            await asyncio.sleep(0.05)
            exit_order.append(idx)

        async def _locked_task(idx):
            async with lock:
                await _dispatch_with_index(idx)

        t1 = asyncio.create_task(_locked_task(1))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(_locked_task(2))
        await asyncio.gather(t1, t2)

        assert enter_order == [1, 2], (
            f"Task 1 must enter before Task 2 (lock serializes); got {enter_order}"
        )
        assert exit_order == [1, 2], (
            f"Task 1 must exit before Task 2; got {exit_order}"
        )


class TestInboxWakeupCrossPoolDefense:
    """_handle_inbox_wakeup must ignore wakeups for sessions not owned by this pool.

    The shared broker keys mailboxes by agent name only.  If two pools use the
    same agent name, a wakeup intended for pool A could be consumed by pool B.
    The defensive check prevents pool B from processing pool A's inbox messages.
    """

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        # This pool only owns the "coding" agent.
        agent_mock = MagicMock()
        agent_mock.stop = AsyncMock()
        p._agents["coding"] = agent_mock
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_handle_inbox_wakeup_skips_foreign_session(self, pool):
        """Wakeup for a session whose agent is not in this pool must be dropped."""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.descriptor import AgentDescriptor

        desc = AgentDescriptor(address=AgentAddress(name="coding"))
        instance = MagicMock()
        instance.descriptor = desc

        # The instance owns "coding", but the wakeup is for a "main" session.
        await pool._handle_inbox_wakeup(instance, "abc123.main")

        # No crash, no processing — simply returns after the defensive check.
        # The real assertion is that we get here without trying to poll/dispatch.

    @pytest.mark.asyncio
    async def test_handle_inbox_wakeup_processes_owned_session(self, pool):
        """Wakeup for a session whose agent is in this pool proceeds to poll."""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.descriptor import AgentDescriptor

        desc = AgentDescriptor(address=AgentAddress(name="coding"))
        instance = MagicMock()
        instance.descriptor = desc

        polled_sessions: list[str] = []

        class _FakeAgentBus:
            async def poll(self, session_id: str, limit: int):
                polled_sessions.append(session_id)
                return []

        pool._agent_bus = _FakeAgentBus()

        await pool._handle_inbox_wakeup(instance, "abc123.coding")
        assert polled_sessions == ["abc123.coding"]


class TestTrackSessionNoDoubleEncode:
    """_track_session must NOT re-encode an already-encoded session_id."""

    async def test_track_session_registers_correct_session_id(self):
        """Regression: _track_session called factory.create with
        external_id=session_id (a full '{prefix}.{agent}' string), causing
        encode_snowflake to double-encode the prefix and produce a different
        session_id — so two session records appeared for one subagent."""
        from framework.core.session_id import SessionIdFactory, encode_snowflake
        from unittest.mock import MagicMock

        factory = SessionIdFactory()
        registry = MagicMock()

        pool = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            session_factory=factory,
            session_registry=registry,
            enable_inbox_polling=False,
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


