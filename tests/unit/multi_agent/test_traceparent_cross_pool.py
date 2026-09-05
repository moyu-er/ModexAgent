"""Tests for traceparent propagation in cross-pool peer sends.

Verifies:
- The :class:`AgentCommunicationService` uses
  :class:`_TracePropagatingPeerNormal` for peer sends.
- Cross-pool send propagates ``TRACEPARENT`` via envelope ``metadata``.
- No traceparent is added when no trace context is active.
- Direct delivery to the peer bus carries the traceparent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker import Address, AddressKind, BrokerMessage, MessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import AgentMessageBus
from modex_agent.multi_agent.communication.service import (
    AgentCommunicationService,
    _resolve_current_traceparent,
    _TracePropagatingPeerNormal,
)
from modex_agent.multi_agent.communication.strategies.base import (
    SendDeps,
    SendRequest,
    SendStrategyKind,
)
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.tools.manager import InMemoryToolManager


def _mock_tree(bus: object) -> SessionTreeManager:
    tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: object) -> None:
        await bus.send(sid, env)  # type: ignore[attr-defined]

    tree.deliver = _deliver
    return tree


_TRACEPARENT = "00-aabbccddeeff00112233445566778899-0011223344556677-01"


class _FakeBus(AgentMessageBus):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, AgentMessageEnvelope]] = []

    async def send(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        self.sent.append((session_id, envelope))

    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[AgentMessageEnvelope]:
        return []

    async def close(self) -> None:
        pass

    async def acknowledge(self, session_id: str, message_id: str) -> None:
        pass

    def release(self, session_id: str, message_ids: list[str]) -> None:
        pass


class _FakeBroker(MessageBroker):
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
        async def _gen() -> AsyncIterator[BrokerMessage]:
            while True:
                yield BrokerMessage(payload={}, sender=Address(kind=AddressKind.AGENT, name="x"))

        return _gen()

    async def send_to(self, recipient: Address, message: BrokerMessage) -> None:
        self.sent.append(message)

    async def publish(self, topic: str, message: BrokerMessage) -> None:
        pass

    async def broadcast(self, message: BrokerMessage) -> None:
        pass

    def subscribe(self, topics: list[str]) -> AsyncIterator[BrokerMessage]:
        async def _gen() -> AsyncIterator[BrokerMessage]:
            while True:
                yield BrokerMessage(payload={}, sender=Address(kind=AddressKind.AGENT, name="x"))

        return _gen()


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


def _make_tree_ref(bus: _FakeBus) -> SessionTreeManager:
    """Mock SessionTreeManager whose deliver() delegates to bus.send()."""
    tree = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: AgentMessageEnvelope) -> None:
        await bus.send(sid, env)

    tree.deliver = _deliver
    return tree


def _make_peer_target(bus: _FakeBus) -> CommunicationTarget:
    return CommunicationTarget(
        name="mainB",
        kind=AgentCommKind.NORMAL,
        pool_name="B",
        tree_ref=_make_tree_ref(bus),
    )


def _make_service(
    *,
    local_bus: _FakeBus | None = None,
) -> tuple[AgentCommunicationService, _FakeBus]:
    local = local_bus or _FakeBus()
    service = AgentCommunicationService(
        source=AgentAddress(name="mainA"),
        registry=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        session_factory=SessionIdFactory(),
    )
    return service, local


@pytest.fixture(autouse=True)
def _clear_traceparent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACEPARENT", raising=False)
    monkeypatch.delenv("TRACESTATE", raising=False)


class TestResolveCurrentTraceparent:
    def test_returns_none_when_no_context(self) -> None:
        assert _resolve_current_traceparent() is None

    def test_returns_from_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRACEPARENT", _TRACEPARENT)
        assert _resolve_current_traceparent() == _TRACEPARENT


class TestTracePropagatingPeerNormalStrategy:
    @pytest.mark.asyncio
    async def test_envelope_carries_traceparent_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRACEPARENT", _TRACEPARENT)
        peer_bus = _FakeBus()
        deps = SendDeps(
            source=AgentAddress(name="mainA"),
            tree=MagicMock(spec=SessionTreeManager),
            session_factory=SessionIdFactory(),
        )
        strategy = _TracePropagatingPeerNormal(deps)
        req = SendRequest(
            target=_make_peer_target(peer_bus),
            content="hello peer",
            invocation_id=None,
            context=_make_context(),
        )

        await strategy.execute(req)

        assert len(peer_bus.sent) == 1
        _sid, envelope = peer_bus.sent[0]
        assert envelope.metadata["traceparent"] == _TRACEPARENT

    @pytest.mark.asyncio
    async def test_envelope_has_no_traceparent_when_none_active(self) -> None:
        peer_bus = _FakeBus()
        deps = SendDeps(
            source=AgentAddress(name="mainA"),
            tree=MagicMock(spec=SessionTreeManager),
            session_factory=SessionIdFactory(),
        )
        strategy = _TracePropagatingPeerNormal(deps)
        req = SendRequest(
            target=_make_peer_target(peer_bus),
            content="hello peer",
            invocation_id=None,
            context=_make_context(),
        )

        await strategy.execute(req)

        assert len(peer_bus.sent) == 1
        _sid, envelope = peer_bus.sent[0]
        assert "traceparent" not in envelope.metadata

    @pytest.mark.asyncio
    async def test_delivery_goes_to_peer_bus_not_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRACEPARENT", _TRACEPARENT)
        peer_bus = _FakeBus()
        local_bus = _FakeBus()
        deps = SendDeps(
            source=AgentAddress(name="mainA"),
            tree=MagicMock(spec=SessionTreeManager),
            session_factory=SessionIdFactory(),
        )
        strategy = _TracePropagatingPeerNormal(deps)
        req = SendRequest(
            target=_make_peer_target(peer_bus),
            content="hello peer",
            invocation_id=None,
            context=_make_context(),
        )

        await strategy.execute(req)

        assert len(peer_bus.sent) == 1
        assert len(local_bus.sent) == 0


class TestServiceCrossPoolTraceparent:
    @pytest.mark.asyncio
    async def test_service_propagates_traceparent_to_peer_bus(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRACEPARENT", _TRACEPARENT)
        service, _local = _make_service()
        peer_bus = _FakeBus()

        ack = await service.send_async(
            target=_make_peer_target(peer_bus),
            content="hello peer",
            invocation_id=None,
            context=_make_context(),
        )

        assert ack
        assert len(peer_bus.sent) == 1
        _sid, envelope = peer_bus.sent[0]
        assert envelope.metadata["traceparent"] == _TRACEPARENT
        assert envelope.message_type == AgentMessageType.AGENT_MESSAGE

    @pytest.mark.asyncio
    async def test_service_no_traceparent_when_none_active(self) -> None:
        service, _local = _make_service()
        peer_bus = _FakeBus()

        await service.send_async(
            target=_make_peer_target(peer_bus),
            content="hello peer",
            invocation_id=None,
            context=_make_context(),
        )

        assert len(peer_bus.sent) == 1
        _sid, envelope = peer_bus.sent[0]
        assert "traceparent" not in envelope.metadata

    def test_service_uses_traced_peer_strategy(self) -> None:
        service, _local = _make_service()
        peer_strategy = service._strategies[SendStrategyKind.PEER_NORMAL]
        assert isinstance(peer_strategy, _TracePropagatingPeerNormal)
