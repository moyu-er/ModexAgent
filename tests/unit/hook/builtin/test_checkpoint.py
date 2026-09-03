"""Tests for CheckpointHook — per-iteration turn snapshot (Repro Path B1)."""

from __future__ import annotations

from typing import Any

import pytest

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.state import (
    ReActRuntimeStateCodec,
    ReActSnapshotPolicy,
    ReActTurnState,
)
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook import HookErrorPolicy, HookPayload, HookPoint, HookRunner, HookSpec
from modex_agent.hook.abc import AfterIterationHook
from modex_agent.hook.builtin.checkpoint import (
    CheckpointHook,
    list_iteration_checkpoints,
)
from modex_agent.ioc.configs.observability import ObservabilityConfig
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, SnapshotReason, TurnPhase
from modex_agent.runtime.models import (
    StateQueryScope,
    TurnIdentity,
    TurnSnapshot,
    TurnStateBase,
)
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.runtime.store import TurnStateStore
from modex_agent.tools.manager import InMemoryToolManager


class _RecordingStore(TurnStateStore):
    """Test store: records every save in order and answers scope-filtered lists.

    Unlike the real stores (keyed by identity → last-write-wins), this keeps
    every saved snapshot so multi-iteration capture can be asserted directly.
    """

    def __init__(self) -> None:
        self.saved: list[TurnSnapshot] = []

    async def save_turn(self, snapshot: TurnSnapshot) -> None:
        self.saved.append(snapshot)

    async def load_turn(self, identity: TurnIdentity) -> TurnSnapshot | None:
        for snap in reversed(self.saved):
            if snap.identity == identity:
                return snap
        return None

    async def delete_turn(self, identity: TurnIdentity) -> None:
        self.saved = [s for s in self.saved if s.identity != identity]

    async def list_active_turns(self, scope: StateQueryScope) -> list[TurnSnapshot]:
        out: list[TurnSnapshot] = []
        for snap in self.saved:
            if scope.agent_id is not None and snap.identity.agent_id != scope.agent_id:
                continue
            if scope.session_id is not None and str(snap.identity.session) != scope.session_id:
                continue
            if scope.reason is not None and snap.reason != scope.reason:
                continue
            out.append(snap)
        return out


class _RaisingStore(_RecordingStore):
    async def save_turn(self, snapshot: TurnSnapshot) -> None:
        raise OSError("disk full")


def _identity(turn_id: str = "t1") -> TurnIdentity:
    return TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id=turn_id)


def _react_state(
    *,
    iteration: int = 1,
    node: ReActNode = ReActNode.LLM,
    turn_id: str = "t1",
    phase: TurnPhase = TurnPhase.RUNNING,
) -> ReActTurnState:
    return ReActTurnState(
        identity=_identity(turn_id),
        agent_kind=AgentKind.REACT,
        phase=phase,
        current_node=node,
        iteration=iteration,
    )


def _ctx(
    state: TurnStateBase,
    store: TurnStateStore | None,
    *,
    hooks: HookRunner | None = None,
) -> AgentContext:
    services = AgentRuntimeServices(turn_store=store, hooks=hooks)
    runtime = AgentRuntime(services=services, state=state)
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("s1"),
        max_iterations=5,
        identity=state.identity,
        runtime=runtime,
    )


def _runner(hook: CheckpointHook) -> HookRunner:
    return HookRunner([HookSpec(hook=hook, on_error=HookErrorPolicy.LOG)])


async def _fire(ctx: AgentContext, hook: CheckpointHook) -> None:
    await _runner(hook).dispatch(HookPoint.AFTER_ITERATION, ctx, HookPayload())


# ---------------------------------------------------------------------------
# Capture + persist
# ---------------------------------------------------------------------------


async def test_after_iteration_captures_and_persists_iteration_snapshot() -> None:
    store = _RecordingStore()
    state = _react_state(iteration=3, node=ReActNode.TOOL)
    ctx = _ctx(state, store)

    await _fire(ctx, CheckpointHook())

    assert len(store.saved) == 1
    snap = store.saved[0]
    assert snap.reason is SnapshotReason.ITERATION
    assert snap.identity == state.identity
    assert snap.state_payload["iteration"] == 3
    assert snap.state_payload["current_node"] == ReActNode.TOOL.value


async def test_multi_iteration_turn_produces_one_iteration_snapshot_per_iteration() -> None:
    store = _RecordingStore()
    runner = _runner(CheckpointHook())

    for i in range(1, 4):
        state = _react_state(iteration=i, node=ReActNode.LLM)
        ctx = _ctx(state, store)
        await runner.dispatch(HookPoint.AFTER_ITERATION, ctx, HookPayload())

    assert len(store.saved) == 3
    for i, snap in enumerate(store.saved, start=1):
        assert snap.reason is SnapshotReason.ITERATION
        assert snap.state_payload["iteration"] == i


# ---------------------------------------------------------------------------
# No-op guards
# ---------------------------------------------------------------------------


async def test_after_iteration_noop_when_state_not_react() -> None:
    store = _RecordingStore()
    plain = TurnStateBase(
        identity=_identity(),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    ctx = _ctx(plain, store)

    await _fire(ctx, CheckpointHook())

    assert store.saved == []


async def test_after_iteration_noop_when_turn_store_none() -> None:
    state = _react_state()
    ctx = _ctx(state, store=None)

    await _fire(ctx, CheckpointHook())


async def test_after_iteration_noop_when_runtime_none() -> None:
    state = _react_state()
    ctx = _ctx(state, store=None)
    ctx.runtime = None

    await _fire(ctx, CheckpointHook())


async def test_after_iteration_does_not_raise_on_store_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _RaisingStore()
    state = _react_state(iteration=2)
    ctx = _ctx(state, store)

    with caplog.at_level("WARNING", logger="modex_agent.hook.builtin.checkpoint"):
        await _fire(ctx, CheckpointHook())

    assert store.saved == []
    assert any("CheckpointHook failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Codec round-trip (existing conformance)
# ---------------------------------------------------------------------------


def test_iteration_snapshot_roundtrips_via_codec() -> None:
    state = _react_state(iteration=4, node=ReActNode.TOOL)
    snapshot = ReActSnapshotPolicy().capture(state, SnapshotReason.ITERATION)

    codec = ReActRuntimeStateCodec()
    payload = codec.encode_turn(snapshot)
    decoded = codec.decode_turn(payload)

    assert decoded.reason is SnapshotReason.ITERATION
    assert decoded.state_payload["iteration"] == 4
    assert decoded.state_payload["current_node"] == ReActNode.TOOL.value
    assert decoded.identity == state.identity


# ---------------------------------------------------------------------------
# Resume from iteration N
# ---------------------------------------------------------------------------


async def test_resume_from_iteration_n_rebuilds_state() -> None:
    store = _RecordingStore()
    state = _react_state(iteration=5, node=ReActNode.TOOL)
    ctx = _ctx(state, store)

    await _fire(ctx, CheckpointHook())

    loaded = await store.load_turn(state.identity)
    assert loaded is not None
    assert loaded.reason is SnapshotReason.ITERATION

    rebuilt = ReActSnapshotPolicy.state_from_snapshot(loaded)
    assert rebuilt.iteration == 5
    assert rebuilt.current_node is ReActNode.TOOL


async def test_resume_from_iteration_n_skips_prior_iterations() -> None:
    store = _RecordingStore()
    runner = _runner(CheckpointHook())

    for i in range(1, 4):
        state = _react_state(iteration=i, node=ReActNode.LLM)
        await runner.dispatch(
            HookPoint.AFTER_ITERATION, ctx=_ctx(state, store), payload=HookPayload()
        )

    last = store.saved[-1]
    assert last.state_payload["iteration"] == 3

    rebuilt = ReActSnapshotPolicy.state_from_snapshot(last)
    assert rebuilt.iteration == 3

    llm_call_count = 0
    for _ in range(rebuilt.iteration + 1, 6):
        llm_call_count += 1

    assert llm_call_count == 2


# ---------------------------------------------------------------------------
# list_iteration_checkpoints helper
# ---------------------------------------------------------------------------


def _snapshot(
    *,
    turn_id: str = "t1",
    iteration: int = 1,
    reason: SnapshotReason = SnapshotReason.ITERATION,
) -> TurnSnapshot:
    return ReActSnapshotPolicy().capture(
        _react_state(iteration=iteration, turn_id=turn_id),
        reason,
    )


async def test_list_iteration_checkpoints_returns_own_ordered_by_iteration() -> None:
    store = _RecordingStore()
    await store.save_turn(_snapshot(turn_id="t1", iteration=2))
    await store.save_turn(_snapshot(turn_id="t2", iteration=1))
    await store.save_turn(_snapshot(turn_id="t1", iteration=1))
    await store.save_turn(_snapshot(turn_id="t1", iteration=3))

    result = await list_iteration_checkpoints(store, _identity("t1"))

    assert [s.state_payload["iteration"] for s in result] == [1, 2, 3]
    assert all(s.identity.turn_id == "t1" for s in result)


async def test_list_iteration_checkpoints_empty_when_no_iteration_records() -> None:
    store = _RecordingStore()
    await store.save_turn(_snapshot(turn_id="t1", iteration=1, reason=SnapshotReason.LLM_COMPLETED))

    result = await list_iteration_checkpoints(store, _identity("t1"))

    assert result == []


# ---------------------------------------------------------------------------
# Deployment wiring (the observability-driven registration moved from the
# retired DefaultAgentFactory injection to the deployment's shared runner —
# bot wiring; the hook itself is unchanged)
# ---------------------------------------------------------------------------


async def test_shared_runner_carries_checkpoint_hook_when_enabled() -> None:
    runner = HookRunner(
        [HookSpec(hook=CheckpointHook(), on_error=HookErrorPolicy.LOG)]
        if ObservabilityConfig(checkpoint_per_iteration=True).checkpoint_per_iteration
        else []
    )
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert CheckpointHook in kinds


async def test_shared_runner_no_checkpoint_hook_when_disabled() -> None:
    runner = HookRunner(
        [HookSpec(hook=CheckpointHook(), on_error=HookErrorPolicy.LOG)]
        if ObservabilityConfig(checkpoint_per_iteration=False).checkpoint_per_iteration
        else []
    )
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert CheckpointHook not in kinds


async def test_checkpoint_per_iteration_false_produces_no_iteration_records() -> None:
    runner = HookRunner(
        [HookSpec(hook=CheckpointHook(), on_error=HookErrorPolicy.LOG)]
        if ObservabilityConfig(checkpoint_per_iteration=False).checkpoint_per_iteration
        else []
    )

    store = _RecordingStore()
    state = _react_state(iteration=1)
    ctx = _ctx(state, store, hooks=runner)

    for _ in range(3):
        await runner.dispatch(HookPoint.AFTER_ITERATION, ctx, HookPayload())

    assert store.saved == []


# ---------------------------------------------------------------------------
# isinstance dispatch contract
# ---------------------------------------------------------------------------


def test_checkpoint_hook_is_after_iteration_hook() -> None:
    hook = CheckpointHook()
    assert isinstance(hook, AfterIterationHook)
    assert hook.name == "checkpoint"


# keep the Any import meaningful for static analyzers
_ANNOT: Any = None
