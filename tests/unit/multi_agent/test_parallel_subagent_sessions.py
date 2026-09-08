"""Tests proving parallel subagent session execution works correctly.

These tests verify the fix for the concurrency issue where multiple calls to the
same subagent template were serialized instead of running in parallel.

Root cause: when a SUBAGENT was already registered, subsequent new-task sends
fell through to the normal delivery path which used send_silent() (no wakeup).
The inbox poller only checked IDLE agents, so messages for WORKING agents were
stuck until the first task completed.

Fix: (1) New tasks to registered subagents use send() (inbox + immediate wakeup).
     (2) Inbox poller checks per-session lock state, not per-agent state.
     (3) Inbox wakeup dispatches messages concurrently via asyncio.create_task.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from modex_agent.core import AgentCommKind
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker import BrokerMessage, MessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.registry import AgentProfile
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.tools.manager import InMemoryToolManager


def _mock_tree(bus: object) -> SessionTreeManager:
    tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: object) -> None:
        await bus.send(sid, env)  # type: ignore[attr-defined]

    tree.deliver = _deliver
    return tree



def _tgt(name: str, kind: AgentCommKind) -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=kind)


# ── Shared test fixtures ──


class _FakeRegistry:
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
        async def _gen() -> None:
            while True:
                await asyncio.sleep(0.1)

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
    parent_session_id: str | None = None,
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
            parent_session_id=parent_session_id,
            metadata=metadata,
        ),
        comm_kind=comm_kind,
    )


def _make_service(
    profiles: list[AgentProfile] | None = None,
    descriptors: list[AgentDescriptor] | None = None,
    agent_bus: _FakeAgentBus | None = None,
    source_name: str = "main",
) -> AgentCommunicationService:
    registry = _FakeRegistry(profiles=profiles, descriptors=descriptors)
    return AgentCommunicationService(
        source=AgentAddress(name=source_name),
        registry=registry,  # type: ignore[arg-type]
        tree=_mock_tree(agent_bus) if agent_bus else MagicMock(spec=SessionTreeManager),
    )


def _subagent_profile(name: str = "scout") -> AgentProfile:
    return AgentProfile(name=name, comm_kind=AgentCommKind.SUBAGENT)


def _subagent_descriptor(name: str = "scout") -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name=name),
        comm_kind=AgentCommKind.SUBAGENT,
    )


def _extract_invocation_id_from_result(text: str) -> str | None:
    """Extract invocation_id from send_async result string."""
    if "invocation_id:" in text:
        return text.split("invocation_id:")[-1].strip().split()[0].rstrip(".")
    return None


# ── Test: Change 1 — New task to registered SUBAGENT uses send() ──


class TestRegisteredSubagentNewTaskUsesSend:
    """Proves that a new task (empty invocation_id) to an already-registered
    SUBAGENT goes through inbox via send() with immediate wakeup, NOT
    send_silent(). This enables concurrent session execution."""

    @pytest.mark.asyncio
    async def test_new_task_uses_send_not_send_silent(self) -> None:
        """New task to registered subagent uses send() for immediate wakeup."""
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_subagent_profile("scout")],
            descriptors=[_subagent_descriptor("scout")],
            agent_bus=bus,
        )
        ctx = _make_context()

        result = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="task 1",
            invocation_id="",
            context=ctx,
        )

        assert "Error" not in result
        # Must use send() (inbox + wakeup), NOT send_silent()
        assert len(bus.sent) == 1, (
            f"Expected send() to be called once, got {len(bus.sent)} calls. "
            "New tasks to registered subagents must use send() for immediate wakeup."
        )
        assert bus.sent_silent == [], (
            "send_silent() should NOT be called for new tasks to registered subagents"
        )

    @pytest.mark.asyncio
    async def test_new_task_gets_unique_invocation_id(self) -> None:
        """Each new task gets a unique invocation_id for session isolation."""
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_subagent_profile("scout")],
            descriptors=[_subagent_descriptor("scout")],
            agent_bus=bus,
        )
        ctx = _make_context()

        result1 = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="task 1",
            invocation_id="",
            context=ctx,
        )
        result2 = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="task 2",
            invocation_id="",
            context=ctx,
        )

        assert "Error" not in result1
        assert "Error" not in result2

        inv1 = _extract_invocation_id_from_result(result1)
        inv2 = _extract_invocation_id_from_result(result2)

        assert inv1 is not None, f"Expected invocation_id in result: {result1}"
        assert inv2 is not None, f"Expected invocation_id in result: {result2}"
        assert inv1 != inv2, "Each new task must get a unique invocation_id"

    @pytest.mark.asyncio
    async def test_new_task_sends_task_request_message_type(self) -> None:
        """New task envelope has message_type='task_request', not 'agent_message'."""
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_subagent_profile("scout")],
            descriptors=[_subagent_descriptor("scout")],
            agent_bus=bus,
        )
        ctx = _make_context()

        result = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="task 1",
            invocation_id="",
            context=ctx,
        )
        assert "Error" not in result
        assert len(bus.sent) == 1

        _session_id, envelope = bus.sent[0]
        assert envelope.message_type == "task_request", (
            f"Expected message_type='task_request', got '{envelope.message_type}'. "
            "New tasks must be dispatched as task_request for proper pipeline handling."
        )

    @pytest.mark.asyncio
    async def test_new_task_returns_invocation_id_in_ack(self) -> None:
        """Acknowledgement string includes the new invocation_id."""
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_subagent_profile("scout")],
            descriptors=[_subagent_descriptor("scout")],
            agent_bus=bus,
        )
        ctx = _make_context()

        result = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="task 1",
            invocation_id="",
            context=ctx,
        )
        assert "invocation_id:" in result, (
            f"New task ack should include invocation_id, got: {result}"
        )


# ── Test: Continuation preserves original send_silent behavior ──


class TestRegisteredSubagentContinuationPreserved:
    """Proves that continuations (existing invocation_id) still use send_silent(),
    preserving backward compatibility."""

    @pytest.mark.asyncio
    async def test_continuation_uses_bus_send(self) -> None:
        """ADR-0015: all sends use bus.send (signals the Drainer)."""
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_subagent_profile("scout")],
            descriptors=[_subagent_descriptor("scout")],
            agent_bus=bus,
        )
        ctx = _make_context()

        result = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="follow-up",
            invocation_id="existing-task-42",
            context=ctx,
        )

        assert "Error" not in result
        assert len(bus.sent) == 1
        assert len(bus.sent_silent) == 0

    @pytest.mark.asyncio
    async def test_continuation_preserves_invocation_id(self) -> None:
        """Continuation keeps the original invocation_id."""
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_subagent_profile("scout")],
            descriptors=[_subagent_descriptor("scout")],
            agent_bus=bus,
        )
        ctx = _make_context()

        result = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="follow-up",
            invocation_id="existing-task-42",
            context=ctx,
        )

        assert "existing-task-42" in result
        assert "invocation_id: existing-task-42" in result


# ── Test: Subagent-to-subagent blocked ──


class TestRegisteredSubagentBlocksSubagentToSubagent:
    """Proves subagent-to-subagent communication is still rejected."""

    @pytest.mark.asyncio
    async def test_subagent_cannot_send_to_another_subagent(self) -> None:
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_subagent_profile("scout")],
            descriptors=[_subagent_descriptor("scout")],
            agent_bus=bus,
        )
        ctx = _make_context(comm_kind=AgentCommKind.SUBAGENT)

        result = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="task",
            invocation_id="",
            context=ctx,
        )

        assert "Error" in result
        assert "subagent" in result.lower()


def _normal_profile(name: str = "main") -> AgentProfile:
    return AgentProfile(name=name, comm_kind=AgentCommKind.NORMAL)


def _normal_descriptor(name: str = "main") -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name=name),
        comm_kind=AgentCommKind.NORMAL,
    )


class TestSubagentBlocksNonParentNormal:
    """Defense-in-depth: a subagent may only address its resolved parent NORMAL
    agent, not any other NORMAL. Parent is recovered from
    ``context.session.parent_session_id`` (production poller path populates it).
    """

    @pytest.mark.asyncio
    async def test_subagent_sending_to_non_parent_normal_is_rejected(self) -> None:
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_normal_profile("main"), _normal_profile("other")],
            descriptors=[_normal_descriptor("main"), _normal_descriptor("other")],
            agent_bus=bus,
        )
        # Subagent whose parent is "main"; it must NOT be able to address
        # sibling NORMAL "other".
        ctx = _make_context(
            agent_name="worker",
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id="conv-1.main",
        )

        result = await svc.send_async(
            target=_tgt("other", AgentCommKind.SUBAGENT),
            content="hi",
            invocation_id=None,
            context=ctx,
        )

        assert "Error" in result
        assert bus.sent == []  # nothing dispatched

    @pytest.mark.asyncio
    async def test_subagent_sending_to_resolved_parent_normal_is_allowed(self) -> None:
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_normal_profile("main")],
            descriptors=[_normal_descriptor("main")],
            agent_bus=bus,
        )
        ctx = _make_context(
            agent_name="worker",
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id="conv-1.main",
        )

        result = await svc.send_async(
            target=_tgt("main", AgentCommKind.NORMAL),
            content="NEED_DECISION: which?",
            invocation_id=None,
            context=ctx,
        )

        assert "Error" not in result
        assert len(bus.sent) == 1

    @pytest.mark.asyncio
    async def test_subagent_without_parent_session_id_still_allowed(self) -> None:
        """Legacy/fallback path: when parent_session_id is unavailable the
        defense is best-effort and must not break the send (documented)."""
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_normal_profile("main")],
            descriptors=[_normal_descriptor("main")],
            agent_bus=bus,
        )
        ctx = _make_context(
            agent_name="worker",
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id=None,
        )

        result = await svc.send_async(
            target=_tgt("main", AgentCommKind.NORMAL),
            content="hi",
            invocation_id=None,
            context=ctx,
        )

        assert "Error" not in result
        assert len(bus.sent) == 1


# ── Test: Two concurrent tasks to same subagent ──


class TestConcurrentSubagentTasks:
    """Proves that two new tasks to the same registered subagent are both
    dispatched with unique sessions via send(), enabling parallel execution."""

    @pytest.mark.asyncio
    async def test_two_tasks_both_dispatched_via_send(self) -> None:
        """Both tasks should be dispatched via send(), each with unique session."""
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_subagent_profile("scout")],
            descriptors=[_subagent_descriptor("scout")],
            agent_bus=bus,
        )
        ctx = _make_context()

        r1 = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="task A",
            invocation_id="",
            context=ctx,
        )
        r2 = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="task B",
            invocation_id="",
            context=ctx,
        )

        assert "Error" not in r1
        assert "Error" not in r2

        # Both used send() (not send_silent)
        assert len(bus.sent) == 2, f"Expected 2 send() calls for 2 tasks, got {len(bus.sent)}"
        assert bus.sent_silent == []

        # Different sessions
        session_ids = {s for s, _ in bus.sent}
        assert len(session_ids) == 2, (
            "Each task should have a unique session_id for parallel execution"
        )

    @pytest.mark.asyncio
    async def test_two_tasks_have_different_invocation_ids(self) -> None:
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_subagent_profile("scout")],
            descriptors=[_subagent_descriptor("scout")],
            agent_bus=bus,
        )
        ctx = _make_context()

        r1 = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="task A",
            invocation_id="",
            context=ctx,
        )
        r2 = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="task B",
            invocation_id="",
            context=ctx,
        )

        inv1 = _extract_invocation_id_from_result(r1)
        inv2 = _extract_invocation_id_from_result(r2)
        assert inv1 is not None
        assert inv2 is not None
        assert inv1 != inv2

    @pytest.mark.asyncio
    async def test_mixed_new_and_continuation(self) -> None:
        """ADR-0015: both new and continuation use bus.send (signals the Drainer)."""
        bus = _FakeAgentBus()
        svc = _make_service(
            profiles=[_subagent_profile("scout")],
            descriptors=[_subagent_descriptor("scout")],
            agent_bus=bus,
        )
        ctx = _make_context()

        # New task -> send()
        r_new = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="new task",
            invocation_id="",
            context=ctx,
        )
        assert "Error" not in r_new
        assert len(bus.sent) == 1
        assert bus.sent_silent == []

        inv_new = _extract_invocation_id_from_result(r_new)
        assert inv_new is not None

        # Continuation — also uses send()
        r_cont = await svc.send_async(
            target=_tgt("scout", AgentCommKind.SUBAGENT),
            content="follow-up",
            invocation_id=inv_new,
            context=ctx,
        )
        assert "Error" not in r_cont
        assert len(bus.sent) == 2, "both uses go through bus.send"
        assert len(bus.sent_silent) == 0, "send_silent is no longer used"
