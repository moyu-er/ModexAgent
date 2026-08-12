"""Characterization tests for subagent instance reuse + communication convergence.

These tests pin down the CURRENT behavior of the poll-driven multi-agent
runtime around four questions:

Q1. Is the subagent system prompt frozen at materialize time?
Q2. Should the prompt be rebuilt per invocation while the instance is reused?
Q3. Is agent reuse isolated per workspace+pool, and can reuse cause path drift?
Q4. Do SendToAgentTool and SubagentAutoSendHook converge on one comm path?

They are written test-first per TDD: tests asserting the *desired* model are
expected to FAIL and reveal the design gap; tests asserting *current* behavior
PASS and document reality.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.scope import MemoryAgentRole
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.ioc.factories.descriptors import build_session_only_memory
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.pool_config.specs import SubagentSpec
from modex_agent.multi_agent.template import AgentTemplate
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


from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver
from modex_agent.tools.presets import ContextMode, SystemPromptMode


# ── helpers ──────────────────────────────────────────────────────────────


def _descriptor(name: str, *, prompt: str | None = None) -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name=name),
        system_prompt_template=prompt,
        comm_kind=AgentCommKind.NORMAL,
    )


def _instance(name: str, *, prompt: str | None = None) -> AgentInstance:
    return AgentInstance(descriptor=_descriptor(name, prompt=prompt), context_manager=MagicMock())


class _FakeForkBuilder:
    """Returns distinct fork XML keyed by parent name, so tests can tell which
    parent's fork context was baked into the system prompt."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def register(self, parent_name: str, xml: str) -> None:
        self._map[parent_name] = xml

    async def build(self, *, parent_session, **_kw) -> str:
        # materialize passes parent_session as SessionInfo|str; the trailing
        # segment is the parent agent name.
        key = str(parent_session).split(".")[-1]
        return self._map.get(key, f"<fork>empty-{key}</fork>")


def _descriptor_of(call) -> AgentDescriptor:
    kwargs = call.kwargs
    if "descriptor" in kwargs:
        return kwargs["descriptor"]
    return call.args[0]


def _deps(
    *,
    pool_get: MagicMock | None = None,
    fork: _FakeForkBuilder | None = None,
    resolver: WorkspacePathResolver | None = None,
    agent_bus: MagicMock | None = None,
) -> tuple[AgentMaterializeDeps, MagicMock]:
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.stop = AsyncMock()
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    pool.get = pool_get or MagicMock(return_value=None)
    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    deps = AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        safety=RuntimeSafetyPolicy(),
        llm_model="gpt-4o",
        project_dir=None,
        agent_bus=agent_bus,
    )
    deps = dataclasses.replace(
        deps,
        context_fork_builder=fork or ContextForkBuilder(),
        workspace_path_resolver=resolver
        or WorkspacePathResolver(workspace_manager=None, pool_name="main"),
    )
    return deps, factory


# ── Q1: is the system prompt frozen at materialize? ─────────────────────


async def _assembled_prompt(ctx_mgr, session_id: str, parent_sid: str | None = None) -> str:
    """load() the context manager for a session and resolve its full pipeline.

    ``parent_sid`` is the authoritative parent link, threaded in via
    runtime_info exactly as dispatch_envelope does at turn time.
    """
    runtime_info = {"parent_session_id": parent_sid} if parent_sid else None
    state = await ctx_mgr.load(session_id=session_id, runtime_info=runtime_info)
    return await state.system_prompt_pipeline.get_or_refresh()


@pytest.mark.asyncio
async def test_materialize_appends_the_current_parent_prompt():
    """APPEND mode now rebuilds per invocation via a provider: a reused instance
    resolves each session's own parent prompt through ``load()``. The parent
    text is NOT in the static ``descriptor.system_prompt_template`` anymore."""
    deps, factory = _deps()
    parent_a = _instance("mainA", prompt="PROMPT_A")
    parent_b = _instance("mainB", prompt="PROMPT_B")
    deps.pool.get = MagicMock(side_effect=lambda n: parent_a if n == "mainA" else parent_b)

    template = AgentTemplate(
        spec=SubagentSpec(agent_name="scout", system_prompt_mode=SystemPromptMode.APPEND)
    )
    await template.materialize(
        parent_session=SessionIdFactory().create(agent_name="mainA"),
        invocation_id="inv1",
        deps=deps,
    )
    ctx_mgr = factory.create_agent.call_args.kwargs["context_manager"]
    static = _descriptor_of(factory.create_agent.call_args).system_prompt_template

    # Parent link now travels via runtime_info (the dispatch envelope path),
    # not a registry resolver. Each session's own parent selects its prompt.
    prompt_a = await _assembled_prompt(ctx_mgr, "inv1.scout", parent_sid="conv.mainA")
    prompt_b = await _assembled_prompt(ctx_mgr, "inv2.scout", parent_sid="conv.mainB")

    # APPEND content lives in the per-session pipeline, not the static template.
    assert "PROMPT_A" not in static
    assert "PROMPT_A" in prompt_a and "PROMPT_B" not in prompt_a
    assert "PROMPT_B" in prompt_b and "PROMPT_A" not in prompt_b


@pytest.mark.asyncio
async def test_materialize_forks_per_parent_via_load():
    """FORK context is rebuilt per invocation via a provider. Each session forks
    its own parent snapshot through ``load()``; the static template carries none
    of it."""
    fork = _FakeForkBuilder()
    fork.register("mainA", "<fork>CONTEXT_A</fork>")
    fork.register("mainB", "<fork>CONTEXT_B</fork>")
    deps, factory = _deps(fork=fork)

    template = AgentTemplate(
        spec=SubagentSpec(
            agent_name="planner",
            context_mode=ContextMode.FORK,
            fork_max_messages=10,
        )
    )
    await template.materialize(
        parent_session=SessionIdFactory().create(agent_name="mainA"),
        invocation_id="inv1",
        deps=deps,
    )
    ctx_mgr = factory.create_agent.call_args.kwargs["context_manager"]
    static = _descriptor_of(factory.create_agent.call_args).system_prompt_template

    prompt_a = await _assembled_prompt(ctx_mgr, "inv1.planner", parent_sid="conv.mainA")
    prompt_b = await _assembled_prompt(ctx_mgr, "inv2.planner", parent_sid="conv.mainB")

    assert "CONTEXT_A" not in static  # not baked
    assert "CONTEXT_A" in prompt_a and "CONTEXT_B" not in prompt_a
    assert "CONTEXT_B" in prompt_b and "CONTEXT_A" not in prompt_b


@pytest.mark.asyncio
async def test_reused_instance_serves_per_invocation_append_and_fork():
    """The direct proof of the fix: ONE materialized instance, loaded for two
    sessions, yields two prompts that each carry their own invocation-specific
    APPEND + FORK — instance reused, prompt rebuilt per invocation."""
    fork = _FakeForkBuilder()
    fork.register("mainA", "<fork>FA</fork>")
    fork.register("mainB", "<fork>FB</fork>")
    deps, factory = _deps(fork=fork)
    parent_a = _instance("mainA", prompt="PA")
    parent_b = _instance("mainB", prompt="PB")
    deps.pool.get = MagicMock(side_effect=lambda n: parent_a if n == "mainA" else parent_b)

    template = AgentTemplate(
        spec=SubagentSpec(
            agent_name="planner",
            system_prompt_mode=SystemPromptMode.APPEND,
            context_mode=ContextMode.FORK,
            fork_max_messages=10,
        )
    )
    await template.materialize(
        parent_session=SessionIdFactory().create(agent_name="mainA"),
        invocation_id="inv1",
        deps=deps,
    )
    ctx_mgr = factory.create_agent.call_args.kwargs["context_manager"]

    a = await _assembled_prompt(ctx_mgr, "inv1.planner", parent_sid="conv.mainA")
    b = await _assembled_prompt(ctx_mgr, "inv2.planner", parent_sid="conv.mainB")

    assert "PA" in a and "FA" in a and "PB" not in a and "FB" not in a
    assert "PB" in b and "FB" in b and "PA" not in b and "FA" not in b


# ── Q2: reuse defeats per-invocation rebuild ────────────────────────────


@pytest.mark.asyncio
async def test_pool_holds_one_instance_per_agent_type():
    pool = AgentPool(broker=MagicMock(), agent_factory=MagicMock())
    try:
        scout_a = _instance("scout", prompt="PROMPT_A")
        scout_b = _instance("scout", prompt="PROMPT_B")
        await pool.register_resident(scout_a.descriptor, scout_a)
        await pool.register_resident(scout_b.descriptor, scout_b)
        # Only one slot: the last registration wins. A's prompt is lost.
        assert pool.get("scout") is scout_b
        assert pool.get("scout").descriptor.system_prompt_template == "PROMPT_B"
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
    prompt_a = await state_a.system_prompt_pipeline.get_or_refresh()
    state_b = await ctx_mgr.load(session_id="invB")
    prompt_b = await state_b.system_prompt_pipeline.get_or_refresh()

    assert "OUTPUT.md" not in prompt_a
    assert "OUTPUT.md" not in prompt_b
    assert "invA" not in prompt_a
    assert "invB" not in prompt_b


# ── Q3: cross-workspace reuse bakes the base dir at materialize ──────────


class _RecordingResolver(WorkspacePathResolver):
    """A WorkspacePathResolver stand-in that returns controlled paths and
    records how many times each accessor is consulted, so tests can prove
    'resolved once at materialize, never re-queried per turn'."""

    def __init__(self, runtime: Path, memory: Path) -> None:
        super().__init__(workspace_manager=None, pool_name="main")
        self._runtime = runtime
        self._memory = memory
        self.runtime_calls = 0
        self.memory_calls = 0

    def runtime_dir(self) -> Path | None:
        self.runtime_calls += 1
        return self._runtime

    def memory_dir(self) -> Path | None:
        self.memory_calls += 1
        return self._memory

    def pruned_manager(self):  # type: ignore[override]
        return None


@pytest.mark.asyncio
async def test_output_base_dir_is_baked_at_materialize_not_per_turn(tmp_path):
    """The OUTPUT *base* dir (workspace runtime dir) is resolved ONCE at
    materialize. A later workspace switch (resolver returns a different
    runtime_dir) does NOT redirect the already-built instance.

    OutputMdProvider is deprecated (T5) — the output path is no longer
    injected into the system prompt. The resolver-baking behavior is still
    verified: the resolver is NOT re-queried during load()."""
    ws_a = tmp_path / "wsA"
    ws_b = tmp_path / "wsB"
    ws_a.mkdir()
    ws_b.mkdir()
    resolver = _RecordingResolver(runtime=ws_a, memory=ws_a / "mem")

    deps, factory = _deps(resolver=resolver)
    template = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
    await template.materialize(
        parent_session=SessionIdFactory().create(agent_name="main"),
        invocation_id="inv1",
        deps=deps,
    )
    ctx_mgr = factory.create_agent.call_args.kwargs["context_manager"]
    runtime_calls_after_materialize = resolver.runtime_calls

    # Simulate a workspace switch: resolver would now report a different root.
    resolver._runtime = ws_b
    state = await ctx_mgr.load(session_id="invX")
    prompt = await state.system_prompt_pipeline.get_or_refresh()

    # OutputMdProvider deprecated — neither workspace path is in the prompt.
    assert str(ws_a) not in prompt
    assert str(ws_b) not in prompt
    assert resolver.runtime_calls == runtime_calls_after_materialize  # no new query


@pytest.mark.asyncio
async def test_memory_workspace_is_baked_at_materialize(tmp_path):
    """The subagent's memory_system is built against the resolver's memory_dir
    AT materialize (build_session_only_memory takes a concrete ``workspace``
    Path, NOT the resolver). After a workspace switch the same instance keeps
    its OLD memory root — the resolver is never re-queried per turn. A second
    drift surface alongside the baked output base dir."""
    ws_a = tmp_path / "wsA"
    ws_a.mkdir()
    resolver = _RecordingResolver(runtime=ws_a, memory=ws_a / "mem")

    deps, factory = _deps(resolver=resolver)
    template = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
    await template.materialize(
        parent_session=SessionIdFactory().create(agent_name="main"),
        invocation_id="inv1",
        deps=deps,
    )
    ctx_mgr = factory.create_agent.call_args.kwargs["context_manager"]
    queries_after_materialize = resolver.memory_calls

    # A per-turn load() must not re-consult the resolver for the memory root.
    await ctx_mgr.load(session_id="inv1")
    assert resolver.memory_calls == queries_after_materialize


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
    await hook._notify_parent(ctx, "inv1.scout", "<subagent_result/>")
    assert bus.send.await_count == 1
    inbox_key, envelope = bus.send.await_args.args
    assert inbox_key == "abc123.main"  # routed to parent's session inbox
    assert envelope.message_type == "agent_result"
