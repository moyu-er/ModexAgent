"""Tests for ParentReplyStrategy in isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker import Address, BrokerMessage, MessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication.strategies.base import SendDeps, SendRequest
from modex_agent.multi_agent.communication.strategies.parent_reply import ParentReplyStrategy
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.tools.manager import InMemoryToolManager


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
    tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)
    if bus is not None:
        async def _deliver(sid: str, env: object) -> None:
            await bus.send(sid, env)  # type: ignore[attr-defined]
        tree.deliver = _deliver
    else:
        tree.deliver = AsyncMock()
    return SendDeps(
        source=AgentAddress(name="worker"),
        session_factory=SessionIdFactory(),
        tree=tree,
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

    def test_build_envelope_appends_answer_contract_for_subagent_sender(self) -> None:
        strategy = ParentReplyStrategy(_make_deps())
        req = _make_request(parent_session_id="conv-1.main")
        session = strategy.build_session(req, "")

        envelope = strategy.build_envelope(req, session, "")
        xml = envelope.payload["content"]

        assert (
            "---\n\n"
            "To answer this subagent, continue its session: call task with\n"
            "target_agent='worker', invocation_id='conv-1', and\n"
            "content=your answer."
        ) in xml

    def test_build_envelope_omits_answer_contract_for_normal_sender(self) -> None:
        strategy = ParentReplyStrategy(_make_deps())
        req = _make_request(comm_kind=AgentCommKind.NORMAL, parent_session_id=None)
        session = strategy.build_session(req, "")

        envelope = strategy.build_envelope(req, session, "")
        xml = envelope.payload["content"]

        assert "---" not in xml
        assert "To answer" not in xml

    def test_build_envelope_external_target_uses_minimal_format(self) -> None:
        """When parent is external (e.g. opencode pool main), the reply XML
        must NOT carry a reply contract. ParentReplyStrategy delegates to
        build_parent_reply_message, which never injects one -- the reply is
        auto-delivered, and the contract would cause a double reply. The
        session-answer block (--- + "To answer this subagent") IS expected:
        it tells the parent how to continue the consultation."""
        from modex_agent.core.agent import ExecutionStrategyKind

        strategy = ParentReplyStrategy(_make_deps())
        req = SendRequest(
            target=CommunicationTarget(
                name="main",
                kind=AgentCommKind.NORMAL,
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
            ),
            content="task done",
            invocation_id=None,
            context=_make_context(
                agent_name="worker",
                comm_kind=AgentCommKind.SUBAGENT,
                parent_session_id="conv-1.main",
            ),
        )
        session = strategy.build_session(req, "")

        envelope = strategy.build_envelope(req, session, "")
        xml = envelope.payload["content"]

        assert "To reply" not in xml
        assert "modexctl send" not in xml
        assert "WARNING" not in xml
        assert "To answer this subagent" in xml

    def test_build_envelope_native_target_uses_minimal_format(self) -> None:
        from modex_agent.core.agent import ExecutionStrategyKind

        strategy = ParentReplyStrategy(_make_deps())
        req = SendRequest(
            target=CommunicationTarget(
                name="main",
                kind=AgentCommKind.NORMAL,
                execution_strategy=ExecutionStrategyKind.REACT,
            ),
            content="task done",
            invocation_id=None,
            context=_make_context(
                agent_name="worker",
                comm_kind=AgentCommKind.SUBAGENT,
                parent_session_id="conv-1.main",
            ),
        )
        session = strategy.build_session(req, "")

        envelope = strategy.build_envelope(req, session, "")
        xml = envelope.payload["content"]

        assert "<reply_contract>" not in xml
        assert "modexctl send" not in xml
