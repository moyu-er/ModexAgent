"""Tests for the per-turn PoolData snapshot resolution.

Unit C: the runner must resolve its per-turn stores (context manager,
turn store) from the active workspace's PoolData snapshot
when a workspace manager is wired, and fall back to its own ``self.*``
stores otherwise.

These tests target ``_resolve_pool_data`` directly (pure resolution
logic, now on :class:`ReActTurnRunner`) and the snapshot-vs-self selection
performed in ``TurnContextBuilder.build_runtime_and_context``. They
construct the runner directly, wiring only the deps the tested paths read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry


class _FakeWorkspace:
    def __init__(self, pool_data: dict[str, PoolDataSnapshot]) -> None:
        self.pool_data = pool_data


@dataclass(frozen=True)
class _FakePoolData(PoolDataSnapshot):
    """Minimal concrete snapshot for the test."""

    context_manager: Any
    turn_store: Any
    trace_store: Any | None = None
    memory_dir: Path | None = None
    runtime_dir: Path | None = None
    pruned_manager: Any | None = None
    experience_dir: Path | None = None


def _make_runner(**attrs: Any) -> ReActTurnRunner:
    """Build a ReActTurnRunner wiring only the deps ``_resolve_pool_data`` /
    ``_is_subagent`` read. The rest default to lightweight fakes so the
    constructor's keyword-only contract is satisfied."""
    defaults: dict[str, Any] = dict(
        agent=MagicMock(name="agent"),
        context_manager=MagicMock(name="context_manager"),
        context_manager_factory=None,
        on_session_start=None,
        on_session_end=None,
        safety=MagicMock(name="safety"),
        turn_store=None,
        registry=TurnSessionRegistry(),
        builder=MagicMock(name="builder"),
        resumer=MagicMock(name="resumer"),
        approval=MagicMock(name="approval"),
        workspace_manager=None,
        pool_name=None,
        pool_data_resolver=None,
        agent_descriptor=None,
    )
    defaults.update(attrs)
    return ReActTurnRunner(**defaults)


def _make_builder(**attrs: Any) -> TurnContextBuilder:
    """Construct a TurnContextBuilder directly for build_runtime_and_context tests.

    The two ``_build_runtime_and_context`` tests previously used ``__new__`` to
    bypass the pipeline constructor; that behavior now lives in
    :class:`TurnContextBuilder`, so these tests construct the builder directly.
    Only the deps the tested path reads are wired; the rest default to None.
    """
    defaults: dict[str, Any] = dict(
        agent=None,
        tool_manager=None,
        sanitizer=None,
        command_processor=None,
        skill_resolver=None,
        context_builder=None,
        agent_descriptor=None,
        max_iterations=5,
        safety=None,
        runtime_services=None,
        runtime_context_manager=None,
        governance=None,
        hook_runner=None,
        interceptor_chain=None,
        control_channel=None,
        emitter_factory=None,
        output_adapter=None,
        turn_store=None,
        registry=TurnSessionRegistry(),
    )
    defaults.update(attrs)
    return TurnContextBuilder(**defaults)


# ---------------------------------------------------------------------------
# _resolve_pool_data
# ---------------------------------------------------------------------------


def test_resolve_pool_data_returns_none_when_no_workspace_manager() -> None:
    """Without a workspace manager, resolution returns None (fallback path)."""
    p = _make_runner(workspace_manager=None, pool_name="main")
    assert p._resolve_pool_data() is None


def test_resolve_pool_data_returns_none_when_no_pool_name() -> None:
    """Workspace manager set but pool_name missing -> None."""
    wm = MagicMock()
    p = _make_runner(workspace_manager=wm, pool_name=None)
    assert p._resolve_pool_data() is None


def test_resolve_pool_data_returns_snapshot_from_active_workspace() -> None:
    """When wired, the snapshot comes from resolve_workspace().pool_data[name]."""
    snap_cm = MagicMock(name="snap_context_manager")
    snap_turn = MagicMock(name="snap_turn_store")
    snapshot = _FakePoolData(
        context_manager=snap_cm,
        turn_store=snap_turn,
    )
    ws = _FakeWorkspace({"main": snapshot})
    wm = MagicMock()
    wm.resolve_workspace.return_value = ws

    p = _make_runner(
        workspace_manager=wm, pool_name="main", agent_descriptor=None,
    )
    resolved = p._resolve_pool_data()

    assert resolved is snapshot
    wm.resolve_workspace.assert_called_once()


def test_resolve_pool_data_returns_none_for_missing_pool() -> None:
    """Pool name not in pool_data -> None (falls back to self.*)."""
    ws = _FakeWorkspace({})
    wm = MagicMock()
    wm.resolve_workspace.return_value = ws

    p = _make_runner(
        workspace_manager=wm, pool_name="main", agent_descriptor=None,
    )
    assert p._resolve_pool_data() is None


def test_resolve_pool_data_returns_snapshot_for_subagent() -> None:
    """A subagent pipeline shares the pool's name with the main agent and
    MUST still resolve the pool's PoolData — it needs the pool-level
    ``turn_store`` so its AgentRuntime is constructed
    and FINALLY_GRAPH hooks (SubagentAutoSendHook) fire. The per-agent
    isolation is enforced one level up: ``_process_message_locked`` does
    not let the snapshot override a subagent's own context_manager.
    """
    from modex_agent.core import AgentCommKind

    snapshot = _FakePoolData(
        context_manager=MagicMock(name="main_context_manager"),
        turn_store=MagicMock(name="main_turn_store"),
    )
    ws = _FakeWorkspace({"main": snapshot})
    wm = MagicMock()
    wm.resolve_workspace.return_value = ws

    subagent_descriptor = MagicMock()
    subagent_descriptor.comm_kind = AgentCommKind.SUBAGENT

    p = _make_runner(
        workspace_manager=wm,
        pool_name="main",
        agent_descriptor=subagent_descriptor,
    )
    assert p._resolve_pool_data() is snapshot


def test_subagent_context_manager_not_overridden_by_pool_data() -> None:
    """The ctx_mgr override lives in _process_message_locked; verify the
    guard predicate directly. A subagent keeps its own context_manager even
    when pool_data is present, while a main agent adopts the pool's.
    """
    from modex_agent.core import AgentCommKind

    sub_desc = MagicMock()
    sub_desc.comm_kind = AgentCommKind.SUBAGENT
    main_desc = MagicMock()
    main_desc.comm_kind = AgentCommKind.NORMAL

    sub_pipe = _make_runner(agent_descriptor=sub_desc)
    main_pipe = _make_runner(agent_descriptor=main_desc)

    assert sub_pipe._is_subagent() is True
    assert main_pipe._is_subagent() is False



def test_resolve_pool_data_returns_snapshot_for_main_agent() -> None:
    """Positive control: a non-subagent (main) pipeline still resolves the
    pool's PoolData so its turns follow workspace switches.
    """
    from modex_agent.core import AgentCommKind

    snapshot = _FakePoolData(
        context_manager=MagicMock(name="main_context_manager"),
        turn_store=MagicMock(name="main_turn_store"),
    )
    ws = _FakeWorkspace({"main": snapshot})
    wm = MagicMock()
    wm.resolve_workspace.return_value = ws

    main_descriptor = MagicMock()
    main_descriptor.comm_kind = AgentCommKind.NORMAL

    p = _make_runner(
        workspace_manager=wm,
        pool_name="main",
        agent_descriptor=main_descriptor,
    )
    assert p._resolve_pool_data() is snapshot



# ---------------------------------------------------------------------------
# _build_runtime_and_context: snapshot stores win over self.*
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_context_uses_snapshot_turn_store_when_wired() -> None:
    """The AgentRuntime built for the turn must use the snapshot's stores."""
    from modex_agent.core.session_id import SessionInfo

    snap_cm = MagicMock(name="snap_cm")
    snap_turn = MagicMock(name="snap_turn_store")
    snapshot = _FakePoolData(
        context_manager=snap_cm,
        turn_store=snap_turn,
    )

    self_turn = MagicMock(name="self_turn_store")  # must NOT be used

    # Minimal context_state stub: only system_prompt / history / pipeline read.
    context_state = MagicMock()
    context_state.system_prompt = "sys"
    context_state.history = MagicMock()
    context_state.system_prompt_pipeline = None

    ctx_mgr = MagicMock()
    ctx_mgr.wrap_governance.return_value = None

    agent = MagicMock()
    agent.name = "main"

    builder = _make_builder(
        agent=agent,
        tool_manager=MagicMock(),
        safety=MagicMock(),
        turn_store=self_turn,
        output_adapter=MagicMock(),
    )

    ctx, _emitter = builder.build_runtime_and_context(
        SessionInfo.from_str("s:main"),
        context_state,
        ctx_mgr,
        pool_data=snapshot,
    )

    # Snapshot injected onto AgentContext
    assert ctx.workspace_snapshot is snapshot
    # The runtime services turn_store comes from the snapshot
    assert ctx.runtime is not None
    assert ctx.runtime.services.turn_store is snap_turn


@pytest.mark.asyncio
async def test_build_context_falls_back_to_self_when_no_snapshot() -> None:
    """Without a snapshot, self.turn_store is used."""
    from modex_agent.core.session_id import SessionInfo

    self_turn = MagicMock(name="self_turn_store")

    context_state = MagicMock()
    context_state.system_prompt = "sys"
    context_state.history = MagicMock()
    context_state.system_prompt_pipeline = None

    ctx_mgr = MagicMock()
    ctx_mgr.wrap_governance.return_value = None

    agent = MagicMock()
    agent.name = "main"

    builder = _make_builder(
        agent=agent,
        tool_manager=MagicMock(),
        safety=MagicMock(),
        turn_store=self_turn,
        output_adapter=MagicMock(),
    )

    ctx, _emitter = builder.build_runtime_and_context(
        SessionInfo.from_str("s:main"),
        context_state,
        ctx_mgr,
        pool_data=None,
    )

    assert ctx.workspace_snapshot is None
    assert ctx.runtime is not None
    assert ctx.runtime.services.turn_store is self_turn


# ---------------------------------------------------------------------------
# _resolve_pool_data — per-turn pool resolution via callable (not static name)
# ---------------------------------------------------------------------------


def test_resolve_pool_data_uses_callable_when_pool_resolver_set() -> None:
    """When ``pool_data_resolver`` is set it takes precedence over static pool_name.

    Regression: pipeline.pool_name was set once during pool init and never
    changed, but a session's pool routing could change between turns (e.g.
    the user switches pools in the WebUI).  With a static pool_name the
    memory system, trace store, and turn store all resolved from
    the WRONG pool — splitting one session's data across multiple pool dirs.
    The resolver callable lets the pipeline ask a per-turn routing source
    (PoolSessionStore) which pool owns this session.
    """
    snap_a = _FakePoolData(
        context_manager=MagicMock(name="cm_a"),
        turn_store=MagicMock(name="ts_a"),
    )
    snap_b = _FakePoolData(
        context_manager=MagicMock(name="cm_b"),
        turn_store=MagicMock(name="ts_b"),
    )
    ws = _FakeWorkspace({"main": snap_a, "coding": snap_b})
    wm = MagicMock()
    wm.resolve_workspace.return_value = ws

    # Resolver callable: per-turn, session_id → pool_name
    calls: list[str] = []
    def pool_resolver(session_id: str) -> str | None:
        calls.append(session_id)
        return "coding" if "coding" in session_id else "main"

    p = _make_runner(
        workspace_manager=wm,
        pool_name=None,
        pool_data_resolver=pool_resolver,
    )

    # Session ending in .coding → pool "coding"
    resolved1 = p._resolve_pool_data("sess.coding")
    assert resolved1 is snap_b
    assert calls == ["sess.coding"]

    # Session ending in .main → pool "main"
    resolved2 = p._resolve_pool_data("sess.main")
    assert resolved2 is snap_a
    assert calls == ["sess.coding", "sess.main"]


def test_resolve_pool_data_falls_back_to_static_pool_name_when_no_resolver() -> None:
    """Without pool_data_resolver the old static pool_name path still works."""
    snapshot = _FakePoolData(
        context_manager=MagicMock(), turn_store=MagicMock(),
    )
    ws = _FakeWorkspace({"main": snapshot})
    wm = MagicMock()
    wm.resolve_workspace.return_value = ws

    p = _make_runner(
        workspace_manager=wm, pool_name="main", pool_data_resolver=None,
    )
    resolved = p._resolve_pool_data()
    assert resolved is snapshot

    # Also works with session_id as an argument (ignored when no resolver).
    resolved2 = p._resolve_pool_data("ignored")
    assert resolved2 is snapshot
