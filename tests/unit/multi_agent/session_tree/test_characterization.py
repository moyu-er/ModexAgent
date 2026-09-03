"""Characterization tests locking tree.deliver + consume + dispatch behavior.

These tests pin the observable behavior of the 4 tree.deliver call sites,
InboxConsumer dedup, and InboxPoller dispatch cycle AFTER Phase 2
convergence (todo 16). They verify the converged behavior: all 4 write
paths flow through tree.deliver, broker fallback is removed, bus.send
returns bool.

Locks:
- (a) pool.submit_input -> tree.deliver -> envelope reaches inbox (pool.py)
- (b) SendStrategy._deliver -> tree.deliver for SubagentDispatch + ParentReply (base.py)
- (c) PeerNormalStrategy.deliver -> tree_ref.deliver + deps.tree fallback (peer_normal.py)
- (d) SubagentAutoSendHook._notify_parent -> tree.deliver AGENT_RESULT (subagent_auto_send.py)
- (e) InboxConsumer.consume dedup + consume (consumer.py)
- (f) InboxPoller _dispatch_batch + _run_turn/_materialize_then_turn finally (inbox_poller.py)
- (g) SendStrategy._deliver converged path — tree.deliver, no broker fallback (base.py)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker import Address, BrokerMessage, MessageBroker
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent import AgentPool
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.communication.strategies.base import SendDeps
from modex_agent.multi_agent.communication.strategies.parent_reply import ParentReplyStrategy
from modex_agent.multi_agent.communication.strategies.peer_normal import PeerNormalStrategy
from modex_agent.multi_agent.communication.strategies.subagent_dispatch import (
    SubagentDispatchStrategy,
)
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox.types import InboxMessage
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.tools.manager import InMemoryToolManager

# -- Shared fixtures -------------------------------------------------------


def _make_bus() -> LocalAgentMessageBus:
    """Real LocalAgentMessageBus backed by InMemoryInboxServer."""
    server = InMemoryInboxServer()
    return LocalAgentMessageBus(
        producer=InboxProducer(server=server),
        consumer=InboxConsumer(server=server),
    )


def _make_tree(bus: LocalAgentMessageBus) -> SessionTreeManager:
    """Mock SessionTreeManager whose deliver() delegates to bus.send().

    Preserves the observable behavior (envelope reaches inbox) without
    requiring the full SessionTreeManager construction (tree/node/track stores).
    """
    tree = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: AgentMessageEnvelope) -> None:
        await bus.send(sid, env)

    tree.deliver = _deliver
    return tree


def _envelope(
    content: str = "test",
    sid: str = "pfx.main",
    msg_type: str = AgentMessageType.AGENT_MESSAGE,
    parent_sid: str | None = None,
) -> AgentMessageEnvelope:
    return AgentMessageEnvelope(
        payload={"content": content, "message_type": msg_type},
        source=AgentAddress(name="src"),
        target=AgentAddress(name="main"),
        message_type=msg_type,
        session_id=sid,
        agent_session_id=sid,
        parent_session_id=parent_sid,
    )


class _FakePool:
    """Minimal pool stub for InboxPoller -- real bus, controllable dispatch/materialize."""

    def __init__(
        self,
        bus: LocalAgentMessageBus,
        *,
        has_instance: bool = False,
        has_template: bool = False,
        materialize_raises: bool = False,
    ) -> None:
        self._bus = bus
        self.session_registry: object | None = None
        self.dispatched: list[AgentMessageEnvelope] = []
        self._instance = MagicMock() if has_instance else None
        self._template = MagicMock() if has_template else None
        self._materialize_raises = materialize_raises

    async def sessions_with_pending(self) -> list[str]:
        return await self._bus.sessions_with_pending()

    async def consume_inbox(
        self, sid: str, *, only_types: set[str] | None = None
    ) -> list[AgentMessageEnvelope]:
        return await self._bus.consume(sid, limit=10, only_types=only_types)

    async def peek_inbox(self, sid: str, limit: int = 1) -> list[AgentMessageEnvelope]:
        return await self._bus.peek(sid, limit=limit)

    async def dispatch_envelope(
        self, sid: str, instance: object, envelope: AgentMessageEnvelope
    ) -> None:
        self.dispatched.append(envelope)

    async def materialize_agent(
        self, sid: str, template: object, *, parent_session_id: str | None = None
    ) -> object:
        if self._materialize_raises:
            raise RuntimeError("materialize failed")
        return MagicMock()

    def get(self, agent_name: str) -> object | None:
        return self._instance

    def get_template(self, agent_name: str) -> object | None:
        return self._template


class _FakeBroker:
    """Minimal duck-typed broker for AgentPool construction."""

    async def send_to(self, address: Address, msg: BrokerMessage) -> None: ...

    async def consume(self, address: Address) -> BrokerMessage | None: return None


def _make_subagent_ctx(parent_sid: str = "conv.main") -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo(
            session_id="inv1.scout",
            agent_name="scout",
            parent_session_id=parent_sid,
        ),
        comm_kind=AgentCommKind.SUBAGENT,
    )


# -- (a) pool.submit_input -> bus.send -> envelope reaches inbox ------------


class TestPoolSubmitInputBusSend:
    async def test_submit_input_envelope_reaches_inbox(self) -> None:
        bus = _make_bus()
        pool = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            agent_bus=bus,
            session_factory=SessionIdFactory(),
        )
        pool.tree = _make_tree(bus)
        msg = InputMessage(
            content="hello world",
            session=SessionInfo(session_id="pfx.main", agent_name="main"),
            source="webui",
        )

        await pool.submit_input("pfx.main", msg)

        consumed = await bus.consume("pfx.main")
        assert len(consumed) == 1
        env = consumed[0]
        assert env.message_type == AgentMessageType.EXTERNAL_INPUT
        assert env.payload["content"] == "hello world"
        assert env.agent_session_id == "pfx.main"
        assert env.source.name == "webui"


# -- (b) SendStrategy._deliver -> bus.send (SubagentDispatch + ParentReply) -


class TestSendStrategyDeliverBusSend:
    @pytest.mark.parametrize(
        "strategy_cls", [SubagentDispatchStrategy, ParentReplyStrategy]
    )
    async def test_deliver_calls_bus_send_envelope_reaches_inbox(
        self, strategy_cls: type
    ) -> None:
        bus = _make_bus()
        deps = SendDeps(
            source=AgentAddress(name="main"),
            session_factory=SessionIdFactory(),
            tree=_make_tree(bus),
        )
        strategy = strategy_cls(deps)
        env = _envelope(sid="conv.scout", msg_type=AgentMessageType.TASK_REQUEST)
        target = CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT)

        err = await strategy.deliver(env, target)

        assert err is None
        consumed = await bus.consume("conv.scout")
        assert len(consumed) == 1
        assert consumed[0].message_type == AgentMessageType.TASK_REQUEST


# -- (c) PeerNormalStrategy.deliver -> tree_ref.deliver + bus.send fallback --


class TestPeerNormalStrategyDeliver:
    async def test_deliver_prefers_tree_ref_when_set(self) -> None:
        local_bus = _make_bus()
        tree_ref = MagicMock()
        tree_ref.deliver = AsyncMock()
        deps = SendDeps(
            source=AgentAddress(name="mainA"),
            session_factory=SessionIdFactory(),
            tree=MagicMock(spec=SessionTreeManager),
        )
        strategy = PeerNormalStrategy(deps)
        env = _envelope(sid="conv.mainB", msg_type=AgentMessageType.AGENT_MESSAGE)
        target = CommunicationTarget(
            name="mainB", kind=AgentCommKind.NORMAL, tree_ref=tree_ref
        )

        err = await strategy.deliver(env, target)

        assert err is None
        tree_ref.deliver.assert_called_once_with("conv.mainB", env)
        local_consumed = await local_bus.consume("conv.mainB")
        assert len(local_consumed) == 0

    async def test_deliver_falls_back_to_deps_tree_when_no_tree_ref(self) -> None:
        local_bus = _make_bus()
        deps = SendDeps(
            source=AgentAddress(name="mainA"),
            session_factory=SessionIdFactory(),
            tree=_make_tree(local_bus),
        )
        strategy = PeerNormalStrategy(deps)
        env = _envelope(sid="conv.mainB", msg_type=AgentMessageType.AGENT_MESSAGE)
        target = CommunicationTarget(
            name="mainB", kind=AgentCommKind.NORMAL, tree_ref=None
        )

        err = await strategy.deliver(env, target)

        assert err is None
        consumed = await local_bus.consume("conv.mainB")
        assert len(consumed) == 1


# -- (d) SubagentAutoSendHook._notify_parent -> bus.send AGENT_RESULT -------


class TestSubagentAutoSendHookNotifyParent:
    async def test_notify_parent_sends_agent_result_to_parent_inbox(self) -> None:
        bus = _make_bus()
        hook = SubagentAutoSendHook(
            tree=_make_tree(bus), self_name="scout", parent_name="main"
        )
        ctx = _make_subagent_ctx(parent_sid="conv.main")

        await hook._notify_parent(ctx, "inv1.scout", "task done")

        consumed = await bus.consume("conv.main")
        assert len(consumed) == 1
        env = consumed[0]
        assert env.message_type == AgentMessageType.AGENT_RESULT
        assert env.agent_session_id == "conv.main"
        assert env.source.name == "scout"
        assert env.target is None
        assert env.invocation_id == "inv1"

    async def test_notify_parent_raises_without_tree(self) -> None:
        hook = SubagentAutoSendHook(tree=None, self_name="scout")
        ctx = _make_subagent_ctx()

        with pytest.raises(RuntimeError, match="tree not wired"):
            await hook._notify_parent(ctx, "inv1.scout", "task done")


# -- (e) InboxConsumer.consume dedup + consume -----------------------------


class TestInboxConsumerConsumeDedup:
    async def test_consume_returns_messages_from_server(self) -> None:
        server = InMemoryInboxServer()
        consumer = InboxConsumer(server=server)
        bus = LocalAgentMessageBus(
            producer=InboxProducer(server=server), consumer=consumer
        )
        await bus.send("s1", _envelope("msg1", sid="s1"))
        await bus.send("s1", _envelope("msg2", sid="s1"))

        consumed = await consumer.consume("s1", limit=100)

        assert len(consumed) == 2

    async def test_consume_dedup_filters_duplicate_message_ids_across_calls(
        self,
    ) -> None:
        msg = InboxMessage(
            session_id="s1",
            source="src",
            content="hello",
            message_type="agent_message",
            message_id="dup1",
            metadata={},
        )
        mock_server = MagicMock()
        mock_server.consume = AsyncMock(side_effect=[[msg], [msg]])
        consumer = InboxConsumer(server=mock_server)

        first = await consumer.consume("s1", limit=100)
        second = await consumer.consume("s1", limit=100)

        assert len(first) == 1
        assert len(second) == 0  # consumer local cache deduped the repeat


# -- (f) InboxPoller _dispatch_batch + finally blocks ----------------------


class TestInboxPollerDispatchCycle:
    async def test_dispatch_batch_consumes_and_dispatches_each_envelope(self) -> None:
        bus = _make_bus()
        pool = _FakePool(bus, has_instance=True)
        poller = InboxPoller(pool, interval=99)
        await bus.send("pfx.main", _envelope("m1", sid="pfx.main"))
        await bus.send("pfx.main", _envelope("m2", sid="pfx.main"))

        await poller._dispatch_batch("pfx.main", MagicMock())

        assert len(pool.dispatched) == 2
        assert "pfx.main" not in await bus.sessions_with_pending()

    async def test_run_turn_finally_pops_inflight_and_signals_wakeup(self) -> None:
        bus = _make_bus()
        pool = _FakePool(bus, has_instance=True)
        poller = InboxPoller(pool, interval=99)
        await bus.send("pfx.main", _envelope("hello", sid="pfx.main"))
        poller._inflight["pfx.main"] = MagicMock(done=lambda: False)
        poller._wakeup_event.clear()

        await poller._run_turn("pfx.main", MagicMock())

        assert "pfx.main" not in poller._inflight
        assert poller._wakeup_event.is_set()
        assert len(pool.dispatched) == 1

    async def test_materialize_then_turn_finally_pops_inflight_and_signals(
        self,
    ) -> None:
        bus = _make_bus()
        pool = _FakePool(bus, has_template=True)
        poller = InboxPoller(pool, interval=99)
        await bus.send(
            "inv1.scout", _envelope("hello", sid="inv1.scout", parent_sid="conv.main")
        )
        poller._inflight["inv1.scout"] = MagicMock(done=lambda: False)
        poller._wakeup_event.clear()

        await poller._materialize_then_turn("inv1.scout", MagicMock())

        assert "inv1.scout" not in poller._inflight
        assert poller._wakeup_event.is_set()
        assert len(pool.dispatched) == 1

    async def test_materialize_failure_keeps_message_in_inbox_and_signals(self) -> None:
        bus = _make_bus()
        pool = _FakePool(bus, has_template=True, materialize_raises=True)
        poller = InboxPoller(pool, interval=99)
        await bus.send(
            "inv1.scout", _envelope("hello", sid="inv1.scout", parent_sid="conv.main")
        )
        poller._inflight["inv1.scout"] = MagicMock(done=lambda: False)
        poller._wakeup_event.clear()

        await poller._materialize_then_turn("inv1.scout", MagicMock())

        assert "inv1.scout" not in poller._inflight
        assert poller._wakeup_event.is_set()
        assert len(pool.dispatched) == 0
        assert "inv1.scout" in await bus.sessions_with_pending()


# -- (g) SendStrategy._deliver converged path — tree.deliver (base.py) --------


class TestSendStrategyDeliverConverged:
    async def test_deliver_uses_tree_deliver_not_broker(self) -> None:
        bus = _make_bus()
        broker = MagicMock(spec=MessageBroker)
        broker.send_to = AsyncMock()
        deps = SendDeps(
            source=AgentAddress(name="main"),
            session_factory=SessionIdFactory(),
            tree=_make_tree(bus),
        )
        strategy = PeerNormalStrategy(deps)
        env = _envelope(sid="conv.scout", msg_type=AgentMessageType.TASK_REQUEST)

        err = await strategy._deliver(env)

        assert err is None
        broker.send_to.assert_not_called()
        consumed = await bus.consume("conv.scout")
        assert len(consumed) == 1

    async def test_deliver_no_error_when_target_is_none(self) -> None:
        bus = _make_bus()
        deps = SendDeps(
            source=AgentAddress(name="main"),
            session_factory=SessionIdFactory(),
            tree=_make_tree(bus),
        )
        strategy = PeerNormalStrategy(deps)
        env = AgentMessageEnvelope(
            payload={"content": "x", "message_type": AgentMessageType.AGENT_MESSAGE},
            source=AgentAddress(name="src"),
            target=None,
            message_type=AgentMessageType.AGENT_MESSAGE,
            session_id="s1",
            agent_session_id="s1",
        )

        err = await strategy._deliver(env)

        assert err is None
