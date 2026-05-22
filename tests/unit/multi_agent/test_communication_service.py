"""Tests for AgentCommunicationService routing logic."""

from __future__ import annotations

import pytest

from framework.core.agent import AgentContext, AgentSessionMeta
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory
from framework.messaging.broker import BrokerMessage, MessageBroker
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.descriptor import AgentDescriptor
from framework.multi_agent.registry import AgentProfile
from framework.multi_agent.communication import AgentCommunicationService
from framework.multi_agent.session_id import DefaultSessionIdStrategy


class _FakeRegistry:
    """Minimal registry for testing communication service routing."""

    def __init__(self, profiles: list[AgentProfile] | None = None, descriptors: list[AgentDescriptor] | None = None) -> None:
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

    def consume_stream(self, address: object):  # type: ignore[no-untyped-def]
        import asyncio

        async def _gen():
            while True:
                await asyncio.sleep(0.1)
                yield BrokerMessage(payload={}, sender=object())  # type: ignore[call-arg]

        return _gen()

    async def send_to(self, recipient: object, message: BrokerMessage) -> None:
        self.sent.append(message)

    async def publish(self, topic: str, message: BrokerMessage) -> None:
        pass

    async def broadcast(self, message: BrokerMessage) -> None:
        pass

    async def subscribe(self, topic: str, address: object) -> None:
        pass


def _make_context(
    conversation_id: str = "conv-1",
    agent_name: str = "main",
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
    uuid: str | None = None,
) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session_meta=AgentSessionMeta(
            conversation_id=conversation_id,
            agent_name=agent_name,
            comm_kind=comm_kind,
            uuid=uuid,
        ),
    )


class TestCommunicationService:
    def _make_service(
        self,
        profiles: list[AgentProfile] | None = None,
        descriptors: list[AgentDescriptor] | None = None,
    ) -> AgentCommunicationService:
        registry = _FakeRegistry(profiles=profiles, descriptors=descriptors)
        broker = _FakeBroker()
        return AgentCommunicationService(
            source=AgentAddress(name="main"),
            broker=broker,
            registry=registry,
        )

    @pytest.mark.asyncio
    async def test_normal_target_no_uuid_routes_correctly(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="main", comm_kind=AgentCommKind.NORMAL)],
            descriptors=[AgentDescriptor(address=AgentAddress(name="main"))],
        )
        ctx = _make_context()
        result = await svc.send_sync(
            target_agent="main", content="hello", uuid=None, context=ctx,
        )
        assert "main" in result

    @pytest.mark.asyncio
    async def test_normal_target_with_empty_uuid_errors(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="reviewer", comm_kind=AgentCommKind.NORMAL)],
            descriptors=[AgentDescriptor(address=AgentAddress(name="reviewer"))],
        )
        ctx = _make_context()
        result = await svc.send_sync(
            target_agent="reviewer", content="hello", uuid="", context=ctx,
        )
        assert "uuid" in result.lower() or "Error" in result or "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_normal_target_with_concrete_uuid_errors(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="reviewer", comm_kind=AgentCommKind.NORMAL)],
            descriptors=[AgentDescriptor(address=AgentAddress(name="reviewer"))],
        )
        ctx = _make_context()
        result = await svc.send_sync(
            target_agent="reviewer", content="hello", uuid="abc123", context=ctx,
        )
        assert "uuid" in result.lower() or "Error" in result or "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_subagent_empty_uuid_creates_task(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT)],
            descriptors=[
                AgentDescriptor(address=AgentAddress(name="office-expert"), comm_kind=AgentCommKind.SUBAGENT),
            ],
        )
        ctx = _make_context()
        result = await svc.send_sync(
            target_agent="office-expert", content="do task", uuid="", context=ctx,
        )
        assert "office-expert" in result
        assert "uuid:" in result

    @pytest.mark.asyncio
    async def test_subagent_existing_uuid_routes_correctly(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT)],
            descriptors=[
                AgentDescriptor(address=AgentAddress(name="office-expert"), comm_kind=AgentCommKind.SUBAGENT),
            ],
        )
        ctx = _make_context()
        result = await svc.send_sync(
            target_agent="office-expert", content="follow-up", uuid="a1b2c3d4", context=ctx,
        )
        assert "office-expert" in result
        assert "a1b2c3d4" in result

    @pytest.mark.asyncio
    async def test_subagent_null_uuid_errors(self) -> None:
        svc = self._make_service(
            profiles=[AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT)],
            descriptors=[
                AgentDescriptor(address=AgentAddress(name="office-expert"), comm_kind=AgentCommKind.SUBAGENT),
            ],
        )
        ctx = _make_context()
        result = await svc.send_sync(
            target_agent="office-expert", content="hello", uuid=None, context=ctx,
        )
        assert "uuid" in result.lower() or "Error" in result or "not found" in result.lower()

    def test_build_targets_description(self) -> None:
        svc = self._make_service(
            profiles=[
                AgentProfile(name="main", comm_kind=AgentCommKind.NORMAL),
                AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT),
            ],
        )
        desc = svc.build_targets_description()
        assert "main" in desc
        assert "office-expert" in desc
        assert "normal" in desc.lower()
        assert "subagent" in desc
