"""Tests for SubagentDispatchStrategy in isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.broker import Address, BrokerMessage, MessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication.strategies.base import SendDeps, SendRequest
from modex_agent.multi_agent.communication.strategies.subagent_dispatch import (
    SubagentDispatchStrategy,
)
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
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


def _make_context(agent_name: str = "main") -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(f"conv-1.{agent_name}"),
        comm_kind=AgentCommKind.NORMAL,
    )


def _make_request(invocation_id: str | None = None) -> SendRequest:
    return SendRequest(
        target=CommunicationTarget(name="worker", kind=AgentCommKind.SUBAGENT),
        content="do work",
        invocation_id=invocation_id,
        context=_make_context(),
    )


def _make_deps(
    bus: object | None = None,
) -> SendDeps:
    return SendDeps(
        source=AgentAddress(name="main"),
        broker=_FakeBroker(),
        session_factory=SessionIdFactory(),
        agent_bus=bus,
    )


class TestSubagentDispatchStrategy:
    @pytest.mark.asyncio
    async def test_execute_mints_invocation_id_and_returns_success(self) -> None:
        bus = _FakeBus()
        strategy = SubagentDispatchStrategy(_make_deps(bus=bus))
        req = _make_request(invocation_id=None)

        result = await strategy.execute(req)

        assert result.error is None
        assert result.target_agent == "worker"
        assert result.target_kind == AgentCommKind.SUBAGENT
        assert result.created_new_task is True
        assert result.invocation_id is not None
        assert result.session_id.endswith(".worker")
        assert len(bus.sent) == 1
        session_id, envelope = bus.sent[0]
        assert session_id == result.session_id
        assert envelope.agent_session_id == result.session_id
        assert envelope.message_type == AgentMessageType.TASK_REQUEST
        assert envelope.invocation_id == result.invocation_id

    @pytest.mark.asyncio
    async def test_workspace_propagated_in_envelope_payload(self) -> None:
        bus = _FakeBus()
        strategy = SubagentDispatchStrategy(_make_deps(bus=bus))
        ws = Path("D:/projects/demo")
        ctx = _make_context()
        ctx.workspace = ws
        req = SendRequest(
            target=CommunicationTarget(name="worker", kind=AgentCommKind.SUBAGENT),
            content="do work",
            invocation_id=None,
            context=ctx,
        )

        result = await strategy.execute(req)

        assert result.error is None
        _, envelope = bus.sent[0]
        assert envelope.payload["workspace"] == str(ws)

        reconstructed = envelope.to_input_message(
            session=SessionInfo.from_str(result.session_id)
        )
        assert reconstructed.workspace == ws

    @pytest.mark.asyncio
    async def test_execute_reuses_existing_invocation_id(self) -> None:
        bus = _FakeBus()
        strategy = SubagentDispatchStrategy(_make_deps(bus=bus))
        req = _make_request(invocation_id="task-42")

        result = await strategy.execute(req)

        assert result.invocation_id == "task-42"
        assert result.created_new_task is False
        assert result.session_id == "task-42.worker"

    @pytest.mark.asyncio
    async def test_deliver_returns_error_without_bus_or_target(self) -> None:
        strategy = SubagentDispatchStrategy(_make_deps())
        envelope = AgentMessageEnvelope(
            payload={"content": "test"},
            source=AgentAddress(name="main"),
            target=None,
            agent_session_id="task-42.worker",
        )

        result = await strategy.deliver(
            envelope, CommunicationTarget(name="worker", kind=AgentCommKind.SUBAGENT)
        )

        assert result is not None
        assert "No target address" in result

    def test_build_session_uses_invocation_id_as_prefix(self) -> None:
        strategy = SubagentDispatchStrategy(_make_deps())
        req = _make_request(invocation_id="task-42")

        session = strategy.build_session(req, "task-42")

        assert session.session_id == "task-42.worker"
        assert session.parent_session_id == str(req.context.session)

    def test_build_envelope_is_task_request(self) -> None:
        strategy = SubagentDispatchStrategy(_make_deps())
        req = _make_request(invocation_id="task-42")
        session = strategy.build_session(req, "task-42")

        envelope = strategy.build_envelope(req, session, "task-42")

        assert envelope.message_type == AgentMessageType.TASK_REQUEST
        assert envelope.agent_session_id == "task-42.worker"
        assert envelope.parent_session_id == str(req.context.session)
        assert envelope.target is not None
        assert envelope.target.name == "worker"
        assert "task-42" in envelope.payload["content"]


class TestBuildResultExecutionStrategyBranch:
    """build_result must branch on CommunicationTarget.execution_strategy.

    Regression: pool_builder creates CommunicationTarget without
    execution_strategy, so external subagents got native ack format
    (Trace/Output paths) instead of external ack format.
    """

    def test_external_subagent_result_has_no_trace_no_output(self) -> None:
        from modex_agent.core.constants import ExecutionStrategyKind

        strategy = SubagentDispatchStrategy(_make_deps())
        req = SendRequest(
            target=CommunicationTarget(
                name="worker",
                kind=AgentCommKind.SUBAGENT,
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
            ),
            content="do work",
            invocation_id="task-1",
            context=_make_context(),
        )
        session = strategy.build_session(req, "task-1")

        result = strategy.build_result(req, session, "task-1")

        assert result.trace_dir is None
        assert result.output_path is None

    def test_external_subagent_ack_uses_external_format(self) -> None:
        from modex_agent.core.constants import ExecutionStrategyKind
        from modex_agent.multi_agent.communication.result import format_send_ack

        strategy = SubagentDispatchStrategy(_make_deps())
        req = SendRequest(
            target=CommunicationTarget(
                name="worker",
                kind=AgentCommKind.SUBAGENT,
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
            ),
            content="do work",
            invocation_id="task-1",
            context=_make_context(),
        )
        session = strategy.build_session(req, "task-1")

        result = strategy.build_result(req, session, "task-1")
        ack = format_send_ack(result)

        assert "modexctl send" in ack
        assert "Trace" not in ack
        assert "Output" not in ack

    def test_default_execution_strategy_is_react(self) -> None:
        """CommunicationTarget defaults to REACT — pool_builder must
        explicitly pass execution_strategy from SubagentSpec for the
        external branch to trigger."""
        from modex_agent.core.constants import ExecutionStrategyKind

        target = CommunicationTarget(name="worker", kind=AgentCommKind.SUBAGENT)
        assert target.execution_strategy == ExecutionStrategyKind.REACT


class TestBuildEnvelopeXmlBranch:
    """build_envelope must emit peer-format XML (with <reply_contract> +
    modexctl send) when target is external, agent-format XML (minimal)
    when target is native.

    Regression: external subagents received the minimal build_agent_message
    format — no reply_contract, no modexctl send instructions — so the
    external CLI had no idea how to reply. Native subagents have
    SubagentAutoSendHook to auto-deliver replies; external CLIs do not.
    """

    def test_external_target_envelope_xml_has_reply_contract_and_modexctl(self) -> None:
        from modex_agent.core.constants import ExecutionStrategyKind

        strategy = SubagentDispatchStrategy(_make_deps())
        req = SendRequest(
            target=CommunicationTarget(
                name="coder",
                kind=AgentCommKind.SUBAGENT,
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
            ),
            content="implement feature X",
            invocation_id="task-1",
            context=_make_context(),
        )
        session = strategy.build_session(req, "task-1")

        envelope = strategy.build_envelope(req, session, "task-1")
        xml = envelope.payload["content"]

        assert "---" in xml
        assert "To reply" in xml
        assert 'modexctl send --to "main"' in xml

    def test_native_target_envelope_xml_is_minimal_no_reply_contract(self) -> None:
        from modex_agent.core.constants import ExecutionStrategyKind

        strategy = SubagentDispatchStrategy(_make_deps())
        req = SendRequest(
            target=CommunicationTarget(
                name="worker",
                kind=AgentCommKind.SUBAGENT,
                execution_strategy=ExecutionStrategyKind.REACT,
            ),
            content="do work",
            invocation_id="task-1",
            context=_make_context(),
        )
        session = strategy.build_session(req, "task-1")

        envelope = strategy.build_envelope(req, session, "task-1")
        xml = envelope.payload["content"]

        assert "<reply_contract>" not in xml
        assert "modexctl send" not in xml
        assert "invocation_id: task-1" in xml
