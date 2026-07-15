"""Tests for SqliteTurnStateStore — save/load/delete/list/find_active + one-active-turn enforcement."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from modex_agent.agents.react.state import (
    ReActRuntimeStateCodec,
    ReActSnapshotPayloadKey,
    ReActSnapshotPolicy,
)
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier
from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters import SqliteTurnStateStore
from modex_agent.runtime.codec import RuntimeStateCodecRegistry
from modex_agent.runtime.enums import (
    AgentKind,
    ApprovalSubjectType,
    SnapshotReason,
    TurnPhase,
)
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ResumePoint,
    StateQueryScope,
    ToolArguments,
    TurnIdentity,
    TurnSnapshot,
)
from modex_agent.runtime.store import ActiveTurnConflictError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteTurnStateStore]:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
    yield SqliteTurnStateStore(manager, registry)
    await manager.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_approval(turn_id: str = "t1") -> ApprovalTransaction:
    return ApprovalTransaction(
        approval_id="appr1",
        turn_id=turn_id,
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch1"],
        requests=[
            ApprovalRequestState(
                request_id="req1",
                approval_id="appr1",
                tool_call_id="call1",
                tool_name="write_file",
                arguments=ToolArguments(values={"path": "/tmp/x"}),
                tier=ApprovalTier.DANGEROUS,
                iteration=1,
            ),
        ],
    )


def _make_snapshot(
    *,
    agent_id: str = "main",
    session_id: str = "s1.main",
    turn_id: str = "t1",
    phase: TurnPhase = TurnPhase.SUSPENDED,
    reason: SnapshotReason = SnapshotReason.TOOL_APPROVAL_REQUIRED,
    approval: ApprovalTransaction | None = None,
) -> TurnSnapshot:
    identity = TurnIdentity(
        agent_id=agent_id,
        session=SessionInfo.from_str(session_id),
        turn_id=turn_id,
    )
    state_payload: dict[str, Any] = {
        ReActSnapshotPayloadKey.CURRENT_NODE.value: "tool",
        ReActSnapshotPayloadKey.ITERATION.value: 1,
        ReActSnapshotPayloadKey.TOOL_BATCHES.value: [],
    }
    if approval is not None:
        state_payload[ReActSnapshotPayloadKey.APPROVAL.value] = (
            ReActSnapshotPolicy.serialize_approval(approval)
        )
    return TurnSnapshot(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=phase,
        reason=reason,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=phase),
        message_delta=[],
        state_payload=state_payload,
    )


# ---------------------------------------------------------------------------
# save → load roundtrip
# ---------------------------------------------------------------------------


async def test_save_load_roundtrip_preserves_all_fields(store: SqliteTurnStateStore) -> None:
    approval = _make_approval(turn_id="t1")
    snapshot = _make_snapshot(
        turn_id="t1",
        phase=TurnPhase.SUSPENDED,
        reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        approval=approval,
    )

    await store.save_turn(snapshot)
    loaded = await store.load_turn(snapshot.identity)

    assert loaded is not None
    assert loaded.identity == snapshot.identity
    assert loaded.agent_kind == AgentKind.REACT
    assert loaded.phase == TurnPhase.SUSPENDED
    assert loaded.reason == SnapshotReason.TOOL_APPROVAL_REQUIRED
    assert loaded.schema_version == snapshot.schema_version
    assert loaded.created_at == pytest.approx(snapshot.created_at)


async def test_save_load_roundtrip_preserves_approval_transaction(
    store: SqliteTurnStateStore,
) -> None:
    approval = _make_approval(turn_id="t1")
    snapshot = _make_snapshot(turn_id="t1", approval=approval)

    await store.save_turn(snapshot)
    loaded = await store.load_turn(snapshot.identity)

    assert loaded is not None
    restored = ReActSnapshotPolicy.approval_from_snapshot(loaded)
    assert restored is not None
    assert restored.approval_id == "appr1"
    assert restored.turn_id == "t1"
    assert restored.subject_type == ApprovalSubjectType.TOOL_BATCH
    assert restored.subject_ids == ["batch1"]
    assert len(restored.requests) == 1
    req = restored.requests[0]
    assert req.tool_name == "write_file"
    assert req.tier == ApprovalTier.DANGEROUS
    assert req.arguments.values == {"path": "/tmp/x"}


async def test_load_turn_returns_none_for_missing(store: SqliteTurnStateStore) -> None:
    identity = TurnIdentity(
        agent_id="main",
        session=SessionInfo.from_str("s1.main"),
        turn_id="nonexistent",
    )
    assert await store.load_turn(identity) is None


# ---------------------------------------------------------------------------
# One-active-turn enforcement (partial unique index)
# ---------------------------------------------------------------------------


async def test_second_running_turn_rejected(store: SqliteTurnStateStore) -> None:
    first = _make_snapshot(turn_id="t1", phase=TurnPhase.RUNNING)
    second = _make_snapshot(turn_id="t2", phase=TurnPhase.RUNNING)

    await store.save_turn(first)
    with pytest.raises(ActiveTurnConflictError):
        await store.save_turn(second)


async def test_second_suspended_turn_rejected(store: SqliteTurnStateStore) -> None:
    first = _make_snapshot(turn_id="t1", phase=TurnPhase.RUNNING)
    second = _make_snapshot(turn_id="t2", phase=TurnPhase.SUSPENDED)

    await store.save_turn(first)
    with pytest.raises(ActiveTurnConflictError):
        await store.save_turn(second)


async def test_running_then_suspended_for_other_agent_session_allowed(
    store: SqliteTurnStateStore,
) -> None:
    first = _make_snapshot(
        agent_id="main", session_id="s1.main", turn_id="t1", phase=TurnPhase.RUNNING
    )
    second = _make_snapshot(
        agent_id="other", session_id="s2.other", turn_id="t2", phase=TurnPhase.RUNNING
    )

    await store.save_turn(first)
    await store.save_turn(second)


async def test_upsert_same_turn_id_does_not_conflict(store: SqliteTurnStateStore) -> None:
    """Saving the same turn_id twice (running → suspended) is an upsert, not a conflict."""
    running = _make_snapshot(turn_id="t1", phase=TurnPhase.RUNNING)
    suspended = _make_snapshot(turn_id="t1", phase=TurnPhase.SUSPENDED)

    await store.save_turn(running)
    await store.save_turn(suspended)

    loaded = await store.load_turn(suspended.identity)
    assert loaded is not None
    assert loaded.phase == TurnPhase.SUSPENDED


# ---------------------------------------------------------------------------
# Completed turn cleanup
# ---------------------------------------------------------------------------


async def test_completed_turn_allows_new_active_turn(
    store: SqliteTurnStateStore,
) -> None:
    running = _make_snapshot(turn_id="t1", phase=TurnPhase.RUNNING)
    completed = _make_snapshot(
        turn_id="t1", phase=TurnPhase.COMPLETED, reason=SnapshotReason.LLM_COMPLETED
    )
    new_active = _make_snapshot(turn_id="t2", phase=TurnPhase.RUNNING)

    await store.save_turn(running)
    await store.save_turn(completed)
    await store.save_turn(new_active)

    loaded = await store.load_turn(new_active.identity)
    assert loaded is not None
    assert loaded.phase == TurnPhase.RUNNING


async def test_delete_completed_turn_cleans_up(store: SqliteTurnStateStore) -> None:
    completed = _make_snapshot(
        turn_id="t1", phase=TurnPhase.COMPLETED, reason=SnapshotReason.LLM_COMPLETED
    )

    await store.save_turn(completed)
    assert await store.load_turn(completed.identity) is not None

    await store.delete_turn(completed.identity)
    assert await store.load_turn(completed.identity) is None


async def test_delete_nonexistent_turn_is_noop(store: SqliteTurnStateStore) -> None:
    identity = TurnIdentity(
        agent_id="ghost",
        session=SessionInfo.from_str("s1.main"),
        turn_id="never",
    )
    await store.delete_turn(identity)


# ---------------------------------------------------------------------------
# Suspend → restart → resume scenario
# ---------------------------------------------------------------------------


async def test_suspend_restart_resume_scenario(store: SqliteTurnStateStore) -> None:
    """Simulates: turn starts running → suspends for approval → resume completes → cleanup."""
    # 1. Turn starts running
    running = _make_snapshot(turn_id="t1", phase=TurnPhase.RUNNING)
    await store.save_turn(running)

    # 2. Turn suspends for approval (same turn_id, phase → suspended)
    approval = _make_approval(turn_id="t1")
    suspended = _make_snapshot(turn_id="t1", phase=TurnPhase.SUSPENDED, approval=approval)
    await store.save_turn(suspended)

    # 3. find_active_turn locates the suspended turn for resume
    found = await store.find_active_turn("main", "s1.main")
    assert found is not None
    assert found.identity.turn_id == "t1"
    assert found.phase == TurnPhase.SUSPENDED

    restored_approval = ReActSnapshotPolicy.approval_from_snapshot(found)
    assert restored_approval is not None
    assert restored_approval.approval_id == "appr1"

    # 4. Resume: apply decision, mark completed, delete the snapshot
    restored_approval.apply_decision("call1", ApprovalDecision.ALLOWED)
    completed = _make_snapshot(
        turn_id="t1",
        phase=TurnPhase.COMPLETED,
        reason=SnapshotReason.LLM_COMPLETED,
    )
    await store.save_turn(completed)

    # 5. After completion, no active turn remains
    assert await store.find_active_turn("main", "s1.main") is None

    # 6. Cleanup: delete the completed turn
    await store.delete_turn(completed.identity)
    assert await store.load_turn(completed.identity) is None


# ---------------------------------------------------------------------------
# find_active_turn
# ---------------------------------------------------------------------------


async def test_find_active_turn_returns_suspended(store: SqliteTurnStateStore) -> None:
    snapshot = _make_snapshot(turn_id="t1", phase=TurnPhase.SUSPENDED)
    await store.save_turn(snapshot)

    found = await store.find_active_turn("main", "s1.main")
    assert found is not None
    assert found.identity.turn_id == "t1"


async def test_find_active_turn_returns_running(store: SqliteTurnStateStore) -> None:
    snapshot = _make_snapshot(turn_id="t1", phase=TurnPhase.RUNNING)
    await store.save_turn(snapshot)

    found = await store.find_active_turn("main", "s1.main")
    assert found is not None
    assert found.phase == TurnPhase.RUNNING


async def test_find_active_turn_returns_none_for_completed(
    store: SqliteTurnStateStore,
) -> None:
    completed = _make_snapshot(
        turn_id="t1", phase=TurnPhase.COMPLETED, reason=SnapshotReason.LLM_COMPLETED
    )
    await store.save_turn(completed)

    assert await store.find_active_turn("main", "s1.main") is None


async def test_find_active_turn_returns_none_when_empty(
    store: SqliteTurnStateStore,
) -> None:
    assert await store.find_active_turn("main", "s1.main") is None


# ---------------------------------------------------------------------------
# list_active_turns
# ---------------------------------------------------------------------------


async def test_list_active_turns_filters_by_agent_and_session(
    store: SqliteTurnStateStore,
) -> None:
    snap_a = _make_snapshot(
        agent_id="main", session_id="s1.main", turn_id="t1", phase=TurnPhase.SUSPENDED
    )
    snap_b = _make_snapshot(
        agent_id="other", session_id="s2.other", turn_id="t2", phase=TurnPhase.SUSPENDED
    )
    await store.save_turn(snap_a)
    await store.save_turn(snap_b)

    results = await store.list_active_turns(StateQueryScope(agent_id="main", session_id="s1.main"))
    assert len(results) == 1
    assert results[0].identity.turn_id == "t1"


async def test_list_active_turns_filters_by_phase(store: SqliteTurnStateStore) -> None:
    suspended = _make_snapshot(turn_id="t1", phase=TurnPhase.SUSPENDED)
    completed = _make_snapshot(
        turn_id="t2", phase=TurnPhase.COMPLETED, reason=SnapshotReason.LLM_COMPLETED
    )
    await store.save_turn(suspended)
    await store.save_turn(completed)

    results = await store.list_active_turns(
        StateQueryScope(agent_id="main", session_id="s1.main", phase=TurnPhase.SUSPENDED)
    )
    assert len(results) == 1
    assert results[0].identity.turn_id == "t1"

    completed_results = await store.list_active_turns(
        StateQueryScope(agent_id="main", session_id="s1.main", phase=TurnPhase.COMPLETED)
    )
    assert len(completed_results) == 1
    assert completed_results[0].identity.turn_id == "t2"


async def test_list_active_turns_returns_empty_when_no_match(
    store: SqliteTurnStateStore,
) -> None:
    snap = _make_snapshot(turn_id="t1", phase=TurnPhase.SUSPENDED)
    await store.save_turn(snap)

    results = await store.list_active_turns(
        StateQueryScope(agent_id="nonexistent", session_id="nope.nope")
    )
    assert results == []


async def test_list_active_turns_unscoped_returns_all(store: SqliteTurnStateStore) -> None:
    snap_a = _make_snapshot(turn_id="t1", phase=TurnPhase.SUSPENDED)
    snap_b = _make_snapshot(
        agent_id="other",
        session_id="s2.other",
        turn_id="t2",
        phase=TurnPhase.RUNNING,
    )
    await store.save_turn(snap_a)
    await store.save_turn(snap_b)

    results = await store.list_active_turns(StateQueryScope())
    assert len(results) == 2
    turn_ids = {r.identity.turn_id for r in results}
    assert turn_ids == {"t1", "t2"}
