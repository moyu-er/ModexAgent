"""Regression: _build_communication must wire session_registry into the
AgentCommunicationService so dispatched subagent sessions are registered with
their parent_session_id.

Root cause (systematic-debugging): _build_communication constructed the main
agent's AgentCommunicationService WITHOUT session_registry, so _send's
`if self._session_registry is not None: register(target_session)` guards were
always False. The child session was never registered with its parent, so
AgentPool.recover_parent_session returned None, so materialize ran with
parent_session=None -> SubagentAutoSendHook was never constructed (no hook
reply) AND on_subagent_created was never called (webui tree not folded).

This test drives the PRODUCTION helper (_build_communication) — the existing
e2e bypasses it by building the service manually with session_registry set.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentPool, SessionRetentionPolicy
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentLLMConfig
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import CommunicationTarget


def _tgt(name: str, kind: AgentCommKind) -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=kind)


class _MockFactory:
    """Minimal factory: returns a mock instance with a descriptor."""

    async def create_agent(self, descriptor, **kwargs):
        inst = MagicMock()
        inst.descriptor = descriptor
        inst.pipeline = MagicMock()
        inst.pipeline.process_message = AsyncMock()
        inst.pipeline.hook_runner = None
        inst.pipeline.hooks = []
        return inst


def _descriptor(name: str, comm_kind: AgentCommKind) -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(kind="agent", name=name),
        llm_config=AgentLLMConfig(model="m", temperature=0.0, max_output_tokens=1),
        system_prompt_template="",
        max_iterations=1,
        execution_strategy="react",
        context_strategy="persistent",
        comm_kind=comm_kind,
    )


async def _make_pool(tmp_path: Path) -> tuple[AgentPool, LocalAgentMessageBus, InMemorySessionRegistry]:
    broker = InMemoryMessageBroker()
    await broker.start()
    server = InMemoryInboxServer()
    bus = LocalAgentMessageBus(
        producer=InboxProducer(server=server),
        consumer=InboxConsumer(server=server),
        broker=broker,
    )
    registry = InMemorySessionRegistry()
    pool = AgentPool(
        broker=broker,
        agent_factory=_MockFactory(),
        agent_bus=bus,
        session_factory=SessionIdFactory(),
        retention=SessionRetentionPolicy(),
        session_registry=registry,
    )
    # Register a NORMAL main agent + a SUBAGENT helper so _resolve_target finds
    # helper as a registered subagent (no template needed).
    await pool.register_resident(_descriptor("main", AgentCommKind.NORMAL), MagicMock(pipeline=MagicMock()))
    helper_inst = MagicMock()
    helper_inst.descriptor = _descriptor("helper", AgentCommKind.SUBAGENT)
    await pool.register_resident(_descriptor("helper", AgentCommKind.SUBAGENT), helper_inst)
    return pool, bus, registry


@pytest.mark.asyncio
async def test_build_communication_wires_session_registry(tmp_path):
    """_build_communication forwards session_registry so _send registers the
    parent->child relationship and recover_parent_session resolves."""
    from modex_agent.core.agent import AgentContext
    from bot.service.pool_builder import _build_communication

    pool, bus, registry = await _make_pool(tmp_path)

    # The production helper — must accept and forward session_registry.
    service, _store = _build_communication(
        pool,
        "main",
        pool._broker,
        bus,
        tmp_path,
        "main",
        [],  # no extra templates; helper is a registered subagent
        AgentTemplateRegistry(tmp_path),
        session_registry=registry,
    )

    ctx = AgentContext(
        system_prompt="",
        history=None,  # type: ignore[arg-type]
        tool_manager=None,  # type: ignore[arg-type]
        session=SessionIdFactory().create(agent_name="main"),
        comm_kind=AgentCommKind.NORMAL,
    )
    result = await service._send(
        target=_tgt("helper", AgentCommKind.SUBAGENT),
        content="do something",
        invocation_id="",
        context=ctx,
    )
    assert result and result.session_id, "send did not produce a child session id"

    # The child must be registered WITH its parent — this is the root-cause
    # assertion. The registry still records parent_session_id (for the WebUI
    # session tree / listing); the agent loop just no longer reads it to
    # recover the parent at turn time.
    child = await registry.get(result.session_id)
    assert child is not None and child.parent_session_id, (
        "child session was not registered with parent_session_id — "
        "_build_communication did not wire session_registry into the service"
    )


@pytest.mark.asyncio
async def test_build_communication_restores_subagent_output_path(tmp_path):
    """_build_communication forwards workspace_path_resolver so _send computes
    the subagent OUTPUT.md path, restoring the 'Output (after notification)'
    line in the send_async ack text (parity with main)."""
    from modex_agent.core.agent import AgentContext
    from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver
    from bot.service.pool_builder import _build_communication

    pool, bus, registry = await _make_pool(tmp_path)
    resolver = WorkspacePathResolver(
        workspace_manager=None, pool_name="main", fallback_runtime_dir=tmp_path
    )
    service, _store = _build_communication(
        pool,
        "main",
        pool._broker,
        bus,
        tmp_path,
        "main",
        [],
        AgentTemplateRegistry(tmp_path),
        session_registry=registry,
        workspace_path_resolver=resolver,
    )
    ctx = AgentContext(
        system_prompt="",
        history=None,  # type: ignore[arg-type]
        tool_manager=None,  # type: ignore[arg-type]
        session=SessionIdFactory().create(agent_name="main"),
        comm_kind=AgentCommKind.NORMAL,
    )
    result = await service._send(
        target=_tgt("helper", AgentCommKind.SUBAGENT),
        content="do something",
        invocation_id="",
        context=ctx,
    )
    assert result and result.session_id
    # output_path must be computed for subagent targets so the ack text shows
    # "Output (after notification): <path>" (parity with main).
    assert result.output_path == tmp_path / "output" / result.session_id / "OUTPUT.md"
    assert result.output_path.parent.exists(), "output dir was not created"
    # trace_dir must also be computed so the ack text shows the execution trace
    # path (main's stated intent — "includes trace/output paths").
    assert result.trace_dir == tmp_path / "trace" / result.session_id

    # send_async's ack must explain what each artifact IS, not just give paths:
    # trace = live, runtime-observable execution log; output = final deliverable.
    ack = await service.send_async(
        target=_tgt("helper", AgentCommKind.SUBAGENT), content="more", invocation_id="", context=ctx,
    )
    assert "Trace" in ack and "live execution log" in ack
    assert "operations.jsonl" in ack
    assert "Output" in ack and "final deliverable" in ack
