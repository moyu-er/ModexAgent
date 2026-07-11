"""Tests for ParentReplyStrategy in isolation."""

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
from modex_agent.multi_agent.communication.strategies.parent_reply import ParentReplyStrategy
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


def _make_context(
    agent_name: str = "main",
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
    parent_session_id: str | None = None,
) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo(
            session_id=f"conv-1.{agent_name}",
            agent_name=agent_name,
            parent_session_id=parent_session_id,
        ),
        comm_kind=comm_kind,
    )


def _make_request(
    target_name: str = "main",
    comm_kind: AgentCommKind = AgentCommKind.SUBAGENT,
    parent_session_id: str | None = "conv-1.main",
) -> SendRequest:
    return SendRequest(
        target=CommunicationTarget(name=target_name, kind=AgentCommKind.NORMAL),
        content="done",
        invocation_id=None,
        context=_make_context(
            agent_name="worker",
            comm_kind=comm_kind,
            parent_session_id=parent_session_id,
        ),
    )


def _make_deps(
    bus: object | None = None,
) -> SendDeps:
    return SendDeps(
        source=AgentAddress(name="worker"),
        broker=_FakeBroker(),
        session_factory=SessionIdFactory(),
        agent_bus=bus,
    )


class TestParentReplyStrategy:
    @pytest.mark.asyncio
    async def test_execute_reuses_parent_session_for_subagent_reply(self) -> None:
        bus = _FakeBus()
        strategy = ParentReplyStrategy(_make_deps(bus=bus))
        req = _make_request(parent_session_id="conv-1.main")

        result = await strategy.execute(req)

        assert result.error is None
        assert result.session_id == "conv-1.main"
        assert len(bus.sent) == 1
        session_id, envelope = bus.sent[0]
        assert session_id == "conv-1.main"
        assert envelope.agent_session_id == "conv-1.main"
        assert envelope.message_type == AgentMessageType.AGENT_MESSAGE

    @pytest.mark.asyncio
    async def test_execute_creates_new_session_for_normal_to_normal(self) -> None:
        bus = _FakeBus()
        strategy = ParentReplyStrategy(_make_deps(bus=bus))
        req = _make_request(comm_kind=AgentCommKind.NORMAL, parent_session_id=None)

        result = await strategy.execute(req)

        assert result.error is None
        assert result.session_id != "conv-1.main"
        assert result.session_id.endswith(".main")
        assert len(bus.sent) == 1

    def test_build_envelope_is_agent_message(self) -> None:
        strategy = ParentReplyStrategy(_make_deps())
        req = _make_request(parent_session_id="conv-1.main")
        session = strategy.build_session(req, "")

        envelope = strategy.build_envelope(req, session, "")

        assert envelope.message_type == AgentMessageType.AGENT_MESSAGE
        assert envelope.invocation_id == "conv-1"
        assert envelope.target is not None
        assert envelope.target.name == "main"

    def test_build_envelope_has_no_invocation_id_for_normal_to_normal(self) -> None:
        strategy = ParentReplyStrategy(_make_deps())
        req = _make_request(comm_kind=AgentCommKind.NORMAL, parent_session_id=None)
        session = strategy.build_session(req, "")

        envelope = strategy.build_envelope(req, session, "")

        assert envelope.message_type == AgentMessageType.AGENT_MESSAGE
        assert envelope.invocation_id is None
