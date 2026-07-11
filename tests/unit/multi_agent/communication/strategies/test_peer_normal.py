"""Tests for PeerNormalStrategy in isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker import Address, BrokerMessage, MessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication.strategies.base import SendDeps, SendRequest
from modex_agent.multi_agent.communication.strategies.peer_normal import PeerNormalStrategy
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.tools import CommunicationTarget


class _FakeBroker(MessageBroker):
    """Minimal broker that records sent messages."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[BrokerMessage] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def register_consumer(self, address: Address) -> None:
        pass

    async def unregister_consumer(self, address: Address) -> None:
        pass

    async def consume(self, address: Address) -> BrokerMessage | None:
        return None

    def consume_stream(self, address: Address) -> AsyncIterator[BrokerMessage]:
        import asyncio

        async def _gen() -> AsyncIterator[BrokerMessage]:
            while True:
                await asyncio.sleep(0.1)
                yield BrokerMessage(payload={}, sender=Address(kind="agent", name="x"))

        return _gen()

    async def send_to(self, recipient: Address, message: BrokerMessage) -> None:
        self.sent.append(message)

    async def publish(self, topic: str, message: BrokerMessage) -> None:
        pass

    async def broadcast(self, message: BrokerMessage) -> None:
        pass

    def subscribe(self, topics: list[str]) -> AsyncIterator[BrokerMessage]:
        import asyncio

        async def _gen() -> AsyncIterator[BrokerMessage]:
            while True:
                await asyncio.sleep(0.1)
                yield BrokerMessage(payload={}, sender=Address(kind="agent", name="x"))

        return _gen()


class _FakeBus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send(self, session_id: str, envelope: object) -> None:
        self.sent.append((session_id, envelope))


def _make_context(agent_name: str = "mainA") -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo(
            session_id=f"convA.{agent_name}",
            agent_name=agent_name,
        ),
        comm_kind=AgentCommKind.NORMAL,
    )


def _make_request(
    target_name: str = "mainB",
    pool_name: str = "B",
    bus_ref: _FakeBus | None = None,
) -> SendRequest:
    return SendRequest(
        target=CommunicationTarget(
            name=target_name,
            kind=AgentCommKind.NORMAL,
            pool_name=pool_name,
            bus_ref=bus_ref,
        ),
        content="hello peer",
        invocation_id=None,
        context=_make_context(),
    )


def _make_deps(
    bus: _FakeBus | None = None,
) -> SendDeps:
    return SendDeps(
        source=AgentAddress(name="mainA"),
        broker=_FakeBroker(),
        session_factory=SessionIdFactory(),
        agent_bus=bus,
    )


class TestPeerNormalStrategy:
    @pytest.mark.asyncio
    async def test_execute_builds_session_with_sender_prefix(self) -> None:
        bus = _FakeBus()
        strategy = PeerNormalStrategy(_make_deps(bus=bus))
        req = _make_request()

        result = await strategy.execute(req)

        assert result.error is None
        assert result.session_id == "convA.mainB"
        assert len(bus.sent) == 1
        session_id, _envelope = bus.sent[0]
        assert session_id == "convA.mainB"

    @pytest.mark.asyncio
    async def test_execute_result_has_no_invocation_id(self) -> None:
        bus = _FakeBus()
        strategy = PeerNormalStrategy(_make_deps(bus=bus))
        req = _make_request()

        result = await strategy.execute(req)

        assert result.invocation_id is None
        assert result.created_new_task is False
        assert result.output_path is None
        assert result.trace_dir is None

    @pytest.mark.asyncio
    async def test_build_envelope_has_sender_prefix_but_xml_none(self) -> None:
        strategy = PeerNormalStrategy(_make_deps())
        req = _make_request()
        session = strategy.build_session(req, "convA")

        envelope = strategy.build_envelope(req, session, "convA")

        assert envelope.message_type == AgentMessageType.AGENT_MESSAGE
        assert envelope.agent_session_id == "convA.mainB"
        assert envelope.invocation_id == "convA"
        assert "invocation_id" not in envelope.payload["content"]
        assert '<agent_message source="mainA">' in envelope.payload["content"]

    @pytest.mark.asyncio
    async def test_deliver_prefers_target_bus_ref(self) -> None:
        local_bus = _FakeBus()
        peer_bus = _FakeBus()
        strategy = PeerNormalStrategy(_make_deps(bus=local_bus))
        req = _make_request(bus_ref=peer_bus)
        session = strategy.build_session(req, "convA")
        envelope = strategy.build_envelope(req, session, "convA")

        err = await strategy.deliver(envelope, req.target)

        assert err is None
        assert len(peer_bus.sent) == 1
        assert len(local_bus.sent) == 0
        assert peer_bus.sent[0][0] == "convA.mainB"

    @pytest.mark.asyncio
    async def test_deliver_falls_back_to_local_bus(self) -> None:
        local_bus = _FakeBus()
        strategy = PeerNormalStrategy(_make_deps(bus=local_bus))
        req = _make_request(bus_ref=None)
        session = strategy.build_session(req, "convA")
        envelope = strategy.build_envelope(req, session, "convA")

        err = await strategy.deliver(envelope, req.target)

        assert err is None
        assert len(local_bus.sent) == 1
        assert local_bus.sent[0][0] == "convA.mainB"

    @pytest.mark.asyncio
    async def test_deliver_returns_error_when_no_bus(self) -> None:
        strategy = PeerNormalStrategy(_make_deps())
        req = _make_request(bus_ref=None)
        session = strategy.build_session(req, "convA")
        envelope = strategy.build_envelope(req, session, "convA")

        err = await strategy.deliver(envelope, req.target)

        assert err is not None
        assert "No bus available" in err

    def test_build_session_reuses_sender_prefix_no_parent(self) -> None:
        strategy = PeerNormalStrategy(_make_deps())
        req = _make_request()

        session = strategy.build_session(req, "convA")

        assert session.session_id == "convA.mainB"
        assert session.parent_session_id is None
        assert session.agent_name == "mainB"
