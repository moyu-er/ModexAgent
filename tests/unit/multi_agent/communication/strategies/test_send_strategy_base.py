"""Tests for the SendStrategy.execute template method in the base class.

Covers cross-cutting behavior injected between ``build_envelope`` and
``deliver`` that every concrete strategy inherits.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication.strategies.base import SendDeps, SendRequest
from modex_agent.multi_agent.communication.strategies.subagent_dispatch import (
    SubagentDispatchStrategy,
)
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import CommunicationTarget


class _FakeBus:
    """Records (session_id, envelope) pairs delivered via tree.deliver."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send(self, session_id: str, envelope: object) -> None:
        self.sent.append((session_id, envelope))


def _make_context(graph_instance_id: int | None = None) -> AgentContext:
    ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("conv-1.main"),
        comm_kind=AgentCommKind.NORMAL,
    )
    ctx.graph_instance_id = graph_instance_id
    return ctx


def _make_deps(bus: _FakeBus) -> SendDeps:
    tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: object) -> None:
        await bus.send(sid, env)

    tree.deliver = _deliver
    return SendDeps(
        source=AgentAddress(name="main"),
        session_factory=SessionIdFactory(),
        tree=tree,
    )


class TestExecuteInjectsGraphInstanceId:
    """Site 1: SendStrategy.execute injects graph_instance_id into envelope.metadata."""

    @pytest.mark.asyncio
    async def test_graph_instance_id_injected_into_envelope_metadata(self) -> None:
        """Given ctx.graph_instance_id=42, the delivered envelope carries metadata["graph_instance_id"]=42."""
        bus = _FakeBus()
        strategy = SubagentDispatchStrategy(_make_deps(bus))
        req = SendRequest(
            target=CommunicationTarget(name="worker", kind=AgentCommKind.SUBAGENT),
            content="do work",
            invocation_id="task-42",
            context=_make_context(graph_instance_id=42),
        )

        result = await strategy.execute(req)

        assert result.error is None
        assert len(bus.sent) == 1
        _, envelope = bus.sent[0]
        assert envelope.metadata["graph_instance_id"] == 42

    @pytest.mark.asyncio
    async def test_no_graph_instance_id_when_ctx_none(self) -> None:
        """Given ctx.graph_instance_id=None, the metadata key is absent (no placeholder)."""
        bus = _FakeBus()
        strategy = SubagentDispatchStrategy(_make_deps(bus))
        req = SendRequest(
            target=CommunicationTarget(name="worker", kind=AgentCommKind.SUBAGENT),
            content="do work",
            invocation_id="task-42",
            context=_make_context(graph_instance_id=None),
        )

        result = await strategy.execute(req)

        assert result.error is None
        _, envelope = bus.sent[0]
        assert "graph_instance_id" not in envelope.metadata
