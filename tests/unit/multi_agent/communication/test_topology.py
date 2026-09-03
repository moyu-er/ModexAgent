"""TopologyPolicy gate tests — nested-tree dispatch relaxation (ticket 12).

The star gate's semantics after SPEC §3.2 activation:

- Any agent with declared children can dispatch (not just main) — a
  SUBAGENT sender may address its own declared children
  (``declared_children``, the per-agent store's direct-child entries).
- Still rejected: subagent→subagent that is NOT a declared child, and
  subagent→non-parent NORMAL (the star gate body is intact).
- NORMAL senders remain unconstrained (peer mesh + dispatch).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication.service import AgentCommunicationService
from modex_agent.multi_agent.communication.topology import TopologyPolicy
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.tools.manager import InMemoryToolManager


def _ctx(
    agent_name: str,
    comm_kind: AgentCommKind,
    parent_session_id: str | None = None,
) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo(
            session_id=f"prefix.{agent_name}",
            agent_name=agent_name,
            parent_session_id=parent_session_id,
        ),
        comm_kind=comm_kind,
    )


def _tgt(name: str, kind: AgentCommKind) -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=kind)


class TestTopologyPolicyGate:
    def test_normal_sender_dispatch_to_subagent_allowed(self) -> None:
        # Given — the classic main→child dispatch
        # When / Then
        assert (
            TopologyPolicy.check(
                AgentCommKind.NORMAL,
                _tgt("child", AgentCommKind.SUBAGENT),
                _ctx("main", AgentCommKind.NORMAL),
            )
            is None
        )

    def test_subagent_to_declared_child_allowed(self) -> None:
        # Given — a mid-level agent dispatching its own declared child
        # (SPEC §3.2: any agent with declared children can dispatch)
        # When / Then
        assert (
            TopologyPolicy.check(
                AgentCommKind.SUBAGENT,
                _tgt("leaf", AgentCommKind.SUBAGENT),
                _ctx("mid", AgentCommKind.SUBAGENT, parent_session_id="prefix.main"),
                declared_children=frozenset({"leaf"}),
            )
            is None
        )

    def test_subagent_to_undeclared_subagent_rejected(self) -> None:
        # Given — the sender has NO declared children (a leaf itself, or a
        # mid targeting a sibling's child)
        # When
        err = TopologyPolicy.check(
            AgentCommKind.SUBAGENT,
            _tgt("other-sub", AgentCommKind.SUBAGENT),
            _ctx("mid", AgentCommKind.SUBAGENT, parent_session_id="prefix.main"),
            declared_children=frozenset({"leaf"}),
        )
        # Then — the star gate body holds: subagent→subagent not via the
        # owning agent is rejected
        assert err is not None
        assert "subagent" in err.lower()

    def test_subagent_to_non_parent_normal_rejected(self) -> None:
        # Given — a subagent addressing a NORMAL agent that is not its parent
        # When
        err = TopologyPolicy.check(
            AgentCommKind.SUBAGENT,
            _tgt("stranger", AgentCommKind.NORMAL),
            _ctx("mid", AgentCommKind.SUBAGENT, parent_session_id="prefix.main"),
        )
        # Then
        assert err is not None
        assert "main" in err  # the resolved parent name is named

    def test_subagent_to_parent_normal_allowed(self) -> None:
        # Given — consultation/reply to the assigning parent
        # When / Then
        assert (
            TopologyPolicy.check(
                AgentCommKind.SUBAGENT,
                _tgt("main", AgentCommKind.NORMAL),
                _ctx("mid", AgentCommKind.SUBAGENT, parent_session_id="prefix.main"),
            )
            is None
        )


class _FakeBus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def send(self, session_id: str, envelope: object) -> None:
        self.sent.append((session_id, envelope))


class TestMidLevelDispatchThroughService:
    """The service derives declared children from the sender's per-agent
    store — a mid-level agent's dispatch to its child routes through
    SubagentDispatchStrategy instead of being rejected."""

    @pytest.mark.asyncio
    async def test_mid_level_dispatch_accepted_and_delivered(self) -> None:
        # Given — the mid's per-agent store carries exactly its direct child
        from modex_agent.multi_agent.tools import CommunicationTargetStore

        store = CommunicationTargetStore()
        store.add(
            CommunicationTarget(
                name="leaf",
                kind=AgentCommKind.SUBAGENT,
                description="the leaf",
            )
        )
        bus = _FakeBus()
        tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

        async def _deliver(sid: str, env: object) -> None:
            await bus.send(sid, env)

        tree.deliver = _deliver
        svc = AgentCommunicationService(
            source=AgentAddress(name="mid"),
            registry=MagicMock(),
            tree=tree,
            target_store=store,
        )
        mid_ctx = _ctx(
            "mid", AgentCommKind.SUBAGENT, parent_session_id="prefix.main"
        )

        # When — the mid dispatches a NEW task to its declared child
        result = await svc.send_async(
            target=store.get("leaf") or _tgt("leaf", AgentCommKind.SUBAGENT),
            content="leaf task",
            invocation_id=None,
            context=mid_ctx,
        )

        # Then — dispatched (not an error ack): the task ack carries the
        # minted invocation_id and the envelope landed on the bus.
        assert "Error" not in result
        assert "invocation_id" in result
        assert len(bus.sent) == 1
        inbox_sid, envelope = bus.sent[0]
        # The child session is two-segment: {invocation_id}.leaf (Errata-1),
        # parented by the mid's session — the nested-tree chain.
        agent_sid = str(envelope.agent_session_id)
        assert inbox_sid == agent_sid
        assert agent_sid.split(".") == [envelope.invocation_id, "leaf"]
        assert envelope.session_id == "prefix.mid"  # the sender's session
        assert envelope.parent_session_id == "prefix.mid"

    @pytest.mark.asyncio
    async def test_leaf_service_without_store_still_rejects_subagent_target(
        self,
    ) -> None:
        # Given — a leaf's service (no per-agent store): it has no declared
        # children, so subagent→subagent stays rejected
        bus = _FakeBus()
        tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

        async def _deliver(sid: str, env: object) -> None:
            await bus.send(sid, env)

        tree.deliver = _deliver
        svc = AgentCommunicationService(
            source=AgentAddress(name="leaf"),
            registry=MagicMock(),
            tree=tree,
        )

        # When
        result = await svc.send_async(
            target=_tgt("other", AgentCommKind.SUBAGENT),
            content="help",
            invocation_id=None,
            context=_ctx("leaf", AgentCommKind.SUBAGENT, parent_session_id="prefix.mid"),
        )

        # Then
        assert "Error" in result
        assert bus.sent == []
