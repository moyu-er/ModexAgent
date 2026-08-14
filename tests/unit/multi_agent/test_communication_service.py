"""Tests for AgentCommunicationService routing logic."""

from __future__ import annotations
from unittest.mock import MagicMock

from collections.abc import AsyncIterator

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker import Address, AddressKind, BrokerMessage, MessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.registry import AgentProfile
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager


def _mock_tree(bus: object) -> SessionTreeManager:
    tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: object) -> None:
        await bus.send(sid, env)  # type: ignore[attr-defined]

    tree.deliver = _deliver
    return tree



def _tgt(name: str, kind: AgentCommKind) -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=kind)


class _FakeRegistry:
    """Minimal registry for testing communication service routing."""

    def __init__(
        self,
        profiles: list[AgentProfile] | None = None,
        descriptors: list[AgentDescriptor] | None = None,
    ) -> None:
        self._profiles = profiles or []
        self._descriptors = descriptors or []

    def list_profiles(self, caller: str | None = None) -> list[AgentProfile]:
        return self._profiles

    def get_profile(self, name: str) -> AgentProfile | None:
        for p in self._profiles:
            if p.name == name:
                return p
        return None

    def get_descriptor(self, name: str) -> AgentDescriptor | None:
        for d in self._descriptors:
            if d.address.name == name:
                return d
        return None


class _FakeBroker(MessageBroker):
    """Minimal broker that records sent messages."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[BrokerMessage] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def register_consumer(self, address: object) -> None:
        pass

    async def unregister_consumer(self, address: object) -> None:
        pass

    async def consume(self, address: object) -> BrokerMessage | None:
        return None

    def consume_stream(self, address: Address) -> AsyncIterator[BrokerMessage]:
        import asyncio

        async def _gen() -> AsyncIterator[BrokerMessage]:
            while True:
                await asyncio.sleep(0.1)
                yield BrokerMessage(
                    payload={},
                    sender=Address(kind=AddressKind.AGENT, name="source"),
                )

        return _gen()

    async def send_to(self, recipient: object, message: BrokerMessage) -> None:
        self.sent.append(message)

    async def publish(self, topic: str, message: BrokerMessage) -> None:
        pass

    async def broadcast(self, message: BrokerMessage) -> None:
        pass

    async def subscribe(self, topic: str, address: object) -> None:
        pass


class _FakeAgentBus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.sent_silent: list[tuple[str, object]] = []

    async def send(self, session_id: str, envelope: object) -> None:
        self.sent.append((session_id, envelope))

    async def send_silent(self, session_id: str, envelope: object) -> None:
        self.sent_silent.append((session_id, envelope))


def _make_context(
    session_id: str = "conv-1",
    agent_name: str = "main",
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
    invocation_id: str | None = None,
) -> AgentContext:
    metadata: dict[str, str] = {}
    session_str = f"{session_id}.{agent_name}"
    if invocation_id:
        metadata["invocation_id"] = invocation_id
        session_str = f"{session_str}.{invocation_id}"
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo(
            session_id=session_str,
            agent_name=agent_name,
            metadata=metadata,
        ),
        comm_kind=comm_kind,
    )


class TestCommunicationService:
    def _make_service(
        self,
        profiles: list[AgentProfile] | None = None,
        descriptors: list[AgentDescriptor] | None = None,
        agent_bus: object | None = None,
        source_name: str = "main",
    ) -> AgentCommunicationService:
        registry = _FakeRegistry(profiles=profiles, descriptors=descriptors)
        broker = _FakeBroker()
        return AgentCommunicationService(
            source=AgentAddress(name=source_name),
            registry=registry,
            tree=_mock_tree(agent_bus) if agent_bus else MagicMock(spec=SessionTreeManager),
        )

    @pytest.mark.asyncio
    async def test_normal_target_no_uuid_routes_correctly(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="main", comm_kind=AgentCommKind.NORMAL)],
            descriptors=[AgentDescriptor(address=AgentAddress(name="main"))],
        )
        ctx = _make_context()
        result = await svc.send_async(
            target=_tgt("main", AgentCommKind.NORMAL),
            content="hello",
            invocation_id=None,
            context=ctx,
        )
        assert "main" in result

    @pytest.mark.asyncio
    async def test_normal_target_with_empty_uuid_errors(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="reviewer", comm_kind=AgentCommKind.NORMAL)],
            descriptors=[AgentDescriptor(address=AgentAddress(name="reviewer"))],
        )
        ctx = _make_context()
        result = await svc.send_async(
            target=_tgt("reviewer", AgentCommKind.NORMAL),
            content="hello",
            invocation_id="",
            context=ctx,
        )
        assert "Reply delivered to 'reviewer'." in result

    @pytest.mark.asyncio
    async def test_normal_target_with_concrete_uuid_errors(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="reviewer", comm_kind=AgentCommKind.NORMAL)],
            descriptors=[AgentDescriptor(address=AgentAddress(name="reviewer"))],
        )
        ctx = _make_context()
        result = await svc.send_async(
            target=_tgt("reviewer", AgentCommKind.NORMAL),
            content="hello",
            invocation_id="abc123",
            context=ctx,
        )
        assert "Reply delivered to 'reviewer'." in result

    @pytest.mark.asyncio
    async def test_subagent_empty_uuid_creates_task(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT)],
            descriptors=[
                AgentDescriptor(
                    address=AgentAddress(name="office-expert"), comm_kind=AgentCommKind.SUBAGENT
                ),
            ],
        )
        ctx = _make_context()
        result = await svc.send_async(
            target=_tgt("office-expert", AgentCommKind.SUBAGENT),
            content="do task",
            invocation_id="",
            context=ctx,
        )
        assert "office-expert" in result
        assert "invocation_id:" in result

    @pytest.mark.asyncio
    async def test_subagent_existing_uuid_routes_correctly(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT)],
            descriptors=[
                AgentDescriptor(
                    address=AgentAddress(name="office-expert"), comm_kind=AgentCommKind.SUBAGENT
                ),
            ],
        )
        ctx = _make_context()
        result = await svc.send_async(
            target=_tgt("office-expert", AgentCommKind.SUBAGENT),
            content="follow-up",
            invocation_id="a1b2c3d4",
            context=ctx,
        )
        assert "office-expert" in result
        assert "a1b2c3d4" in result

    @pytest.mark.asyncio
    async def test_async_subagent_send_uses_full_task_session_inbox(self) -> None:
        bus = _FakeAgentBus()
        svc = self._make_service(
            profiles=[AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT)],
            descriptors=[
                AgentDescriptor(
                    address=AgentAddress(name="office-expert"), comm_kind=AgentCommKind.SUBAGENT
                ),
            ],
            agent_bus=bus,
        )
        ctx = _make_context()

        result = await svc.send_async(
            target=_tgt("office-expert", AgentCommKind.SUBAGENT),
            content="follow-up",
            invocation_id="task-42",
            context=ctx,
        )

        assert "task-42" in result
        # ADR-0015: send always uses bus.send (signals the Drainer), not send_silent
        assert len(bus.sent) == 1
        assert len(bus.sent_silent) == 0
        session_id, envelope = bus.sent[0]
        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()
        expected_sid = factory.create_with_prefix(
            agent_name="office-expert",
            prefix="task-42",
            parent_session_id=ctx.session,
        )
        expected_session_id = str(expected_sid)
        assert session_id == expected_session_id
        assert envelope.agent_session_id == expected_session_id
        assert envelope.invocation_id == "task-42"

    @pytest.mark.asyncio
    async def test_subagent_null_uuid_errors(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT)],
            descriptors=[
                AgentDescriptor(
                    address=AgentAddress(name="office-expert"), comm_kind=AgentCommKind.SUBAGENT
                ),
            ],
        )
        ctx = _make_context()
        result = await svc.send_async(
            target=_tgt("office-expert", AgentCommKind.SUBAGENT),
            content="hello",
            invocation_id=None,
            context=ctx,
        )
        assert (
            "invocation_id" in result.lower() or "Error" in result or "not found" in result.lower()
        )

    @pytest.mark.asyncio
    async def test_subagent_consult_routes_to_real_parent_session(self) -> None:
        """A subagent consulting its parent must land in the parent's ACTUAL
        session inbox — not mint a brand-new parent session.

        Regression for the phantom-session bug: a subagent's send_to_agent to
        its parent went through the generic NORMAL branch, which called
        ``session_factory.create(...)`` and minted a fresh snowflake main
        session every time. The real parent never received the consult and a
        duplicate main agent spun up under the phantom id.
        """
        bus = _FakeAgentBus()
        svc = self._make_service(
            profiles=[AgentProfile(name="coding", comm_kind=AgentCommKind.NORMAL)],
            descriptors=[AgentDescriptor(address=AgentAddress(name="coding"))],
            agent_bus=bus,
            source_name="worker",
        )
        parent_session_id = "conv-1.coding"
        worker_session = SessionInfo(
            session_id="task-42.worker",
            agent_name="worker",
            parent_session_id=parent_session_id,
        )
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=worker_session,
            comm_kind=AgentCommKind.SUBAGENT,
        )

        result = await svc.send_async(
            target=_tgt("coding", AgentCommKind.NORMAL),
            content="QUESTION: should I proceed?",
            invocation_id=None,
            context=ctx,
        )

        assert "coding" in result
        assert len(bus.sent) == 1
        inbox_key, envelope = bus.sent[0]
        # MUST route to the real parent session — not a freshly minted snowflake.
        assert inbox_key == parent_session_id
        assert envelope.agent_session_id == parent_session_id

    @pytest.mark.asyncio
    async def test_subagent_cannot_send_directly_to_another_subagent(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="query-12306", comm_kind=AgentCommKind.SUBAGENT)],
            descriptors=[
                AgentDescriptor(
                    address=AgentAddress(name="query-12306"), comm_kind=AgentCommKind.SUBAGENT
                ),
            ],
            source_name="office-expert",
        )
        ctx = _make_context(
            agent_name="office-expert",
            comm_kind=AgentCommKind.SUBAGENT,
            invocation_id="task-42",
        )

        result = await svc.send_async(
            target=_tgt("query-12306", AgentCommKind.SUBAGENT),
            content="please query train info",
            invocation_id="",
            context=ctx,
        )

        assert "Error:" in result
        assert "subagent" in result.lower()
        assert "peer" in result.lower()
