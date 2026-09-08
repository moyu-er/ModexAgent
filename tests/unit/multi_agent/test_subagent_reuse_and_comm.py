"""Characterization tests for subagent instance reuse + communication convergence.

These tests pin down the CURRENT behavior of the poll-driven multi-agent
runtime around four questions:

Q1. Is the subagent system prompt frozen at materialize time?
    → Materialize now routes through ``AssemblyPipeline.run``; prompt
      assembly is owned by the pipeline stages. The per-invocation FORK
      context rebuild is verified in the pipeline stage tests.
Q2. Should the prompt be rebuilt per invocation while the instance is reused?
    → Same as Q1 — pipeline-owned.
Q3. Is agent reuse isolated per workspace+pool, and can reuse cause path drift?
    → Covered by ``test_pool_holds_one_instance_per_agent_type`` and
      ``test_each_pool_has_an_isolated_agent_registry`` below.
Q4. Do SendToAgentTool and SubagentAutoSendHook converge on one comm path?
    → Covered by ``test_send_to_agent_routes_through_tree`` and
      ``test_subagent_auto_send_hook_routes_through_same_bus`` below.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core import AgentCommKind
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.ioc.factories.descriptors import build_session_only_memory
from modex_agent.memory.scope import MemoryAgentRole
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import CommunicationTarget


def _mock_tree(bus: object) -> SessionTreeManager:
    tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: object) -> None:
        await bus.send(sid, env)  # type: ignore[attr-defined]

    tree.deliver = _deliver
    return tree


def _tgt(name: str, kind: AgentCommKind) -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=kind)


# ── helpers ──────────────────────────────────────────────────────────────


def _descriptor(name: str, *, prompt: str | None = None) -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name=name),
        system_prompt_template=prompt,
        comm_kind=AgentCommKind.NORMAL,
    )


def _instance(name: str, *, prompt: str | None = None) -> AgentInstance:
    return AgentInstance(descriptor=_descriptor(name, prompt=prompt), context_manager=MagicMock())


# ── Q3: reuse defeats per-invocation rebuild ────────────────────────────


@pytest.mark.asyncio
async def test_pool_holds_one_instance_per_agent_type():
    pool = AgentPool(broker=MagicMock(), agent_factory=MagicMock())
    try:
        scout_a = _instance("scout", prompt="PROMPT_A")
        scout_b = _instance("scout", prompt="PROMPT_B")
        await pool.register_resident(scout_a.descriptor, scout_a)
        await pool.register_resident(scout_b.descriptor, scout_b)
        # Only one slot: the last registration wins. A's prompt is lost.
        result = pool.get("scout")
        assert result is scout_b
        assert result.descriptor.system_prompt_template == "PROMPT_B"  # type: ignore[union-attr]
    finally:
        await pool.shutdown_all()


# ── Q1 mitigation: OUTPUT path is per-session dynamic (hook-owned) ───────


@pytest.mark.asyncio
async def test_output_md_path_session_leaf_is_dynamic(tmp_path):
    """OutputMdProvider is deprecated (T5); the subagent system prompt no
    longer carries an OUTPUT.md path. The hook (SubagentAutoSendHook) now
    writes numbered OUTPUT_<n>.md files to ``<runtime>/output/<session_id>/``
    at turn end — per-session by construction, not via prompt injection."""
    ctx_mgr = build_session_only_memory(
        cfg=None,
        workspace=tmp_path,
        agent_id="scout",
        agent_role=MemoryAgentRole.SUBAGENT,
        system_prompt="",
        output_base_dir=tmp_path / "output",
    )
    state_a = await ctx_mgr.load(session_id="invA")
    prompt_a = await state_a.system_prompt_pipeline.get_or_refresh()  # type: ignore[union-attr]
    state_b = await ctx_mgr.load(session_id="invB")
    prompt_b = await state_b.system_prompt_pipeline.get_or_refresh()  # type: ignore[union-attr]

    assert "OUTPUT.md" not in prompt_a
    assert "OUTPUT.md" not in prompt_b
    assert "invA" not in prompt_a
    assert "invB" not in prompt_b


# ── Q3: pool-level isolation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_pool_has_an_isolated_agent_registry():
    """Two AgentPool instances carry independent _agents dicts. A subagent
    named 'scout' registered in pool A is NOT visible to pool B. Cross-pool
    reuse does not happen; isolation is at least per-pool-instance."""
    pool_a = AgentPool(broker=MagicMock(), agent_factory=MagicMock())
    pool_b = AgentPool(broker=MagicMock(), agent_factory=MagicMock())
    try:
        scout_a = _instance("scout", prompt="A")
        scout_b = _instance("scout", prompt="B")
        await pool_a.register_resident(scout_a.descriptor, scout_a)
        await pool_b.register_resident(scout_b.descriptor, scout_b)
        assert pool_a.get("scout") is scout_a
        assert pool_b.get("scout") is scout_b
        assert pool_a.get("scout") is not pool_b.get("scout")
    finally:
        await pool_a.shutdown_all()
        await pool_b.shutdown_all()


# ── Q4: communication convergence ────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_to_agent_routes_through_tree():
    """The outbound task_request path (SendToAgentTool -> _send) enqueues via
    tree.deliver when a subagent template matches — same carrier as the
    auto-send hook below."""
    from modex_agent.multi_agent.communication import AgentCommunicationService

    bus = MagicMock()
    bus.send = AsyncMock()
    template_registry = MagicMock()
    template_registry.get_template = MagicMock(return_value=object())  # subagent match
    registry = MagicMock()
    registry.get_descriptor = MagicMock(return_value=None)
    registry.get_profile = MagicMock(return_value=None)
    svc = AgentCommunicationService(
        source=AgentAddress(name="main"),
        registry=registry,
        tree=_mock_tree(bus),
        session_factory=SessionIdFactory(),
        session_registry=AsyncMock(),
        template_registry=template_registry,
        pool_name="main",
    )
    ctx = SimpleNamespace(
        session=SessionIdFactory().create(agent_name="main"),
        comm_kind=AgentCommKind.NORMAL,
        workspace=None,
        graph_instance_id=None,
        runtime=None,
    )
    await svc._send(
        target=_tgt("scout", AgentCommKind.SUBAGENT),
        content="do X",
        invocation_id=None,
        context=ctx,  # type: ignore[arg-type]
    )
    assert bus.send.await_count == 1
    _sid, envelope = bus.send.await_args.args
    assert isinstance(envelope, AgentMessageEnvelope)
    assert envelope.message_type == "task_request"


@pytest.mark.asyncio
async def test_subagent_auto_send_hook_routes_through_same_bus():
    """The inbound result-notification path (SubagentAutoSendHook) enqueues via
    the SAME agent_bus.send carrier — confirming both directions converge on
    one communication implementation, not two parallel mechanisms."""
    bus = MagicMock()
    bus.send = AsyncMock()
    hook = SubagentAutoSendHook(
    tree=_mock_tree(bus),
        self_name="scout",
        parent_name="main",
        runtime_dir=Path("."),
    )
    session = SimpleNamespace(
        __str__=lambda self: "inv1.scout",
        session_id_prefix="inv1",
        parent_session_id="abc123.main",
    )
    ctx = SimpleNamespace(session=session, graph_instance_id=None)
    await hook._notify_parent(ctx, "inv1.scout", "<subagent_result/>")  # type: ignore[arg-type]
    assert bus.send.await_count == 1
    inbox_key, envelope = bus.send.await_args.args
    assert inbox_key == "abc123.main"  # routed to parent's session inbox
    assert envelope.message_type == "agent_result"
