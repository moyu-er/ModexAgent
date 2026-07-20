"""Tests for :class:`SqliteDecisionCoordinator` — atomic snapshot + audit write."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modex_agent.agents.react.state import (
    ReActRuntimeStateCodec,
)
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.approval_audit_store import (
    SqliteApprovalAuditStore,
)
from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore
from modex_agent.persistence.coordinator import SqliteDecisionCoordinator
from modex_agent.runtime.approval_decision import (
    ApprovalAuditDecision,
    ApprovalAuditEntry,
)
from modex_agent.runtime.codec import RuntimeStateCodecRegistry
from modex_agent.runtime.enums import AgentKind, SnapshotReason, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import (
    JsonValue,
    ResumePoint,
    TurnIdentity,
    TurnSnapshot,
)
from modex_agent.runtime.store import ActiveTurnConflictError


class _PoolScopedRecordScope(RecordScope):
    """Test-local ``RecordScope`` subclass adding the pool dimension.

    ADR-0028 removed ``pool`` from the framework's base ``RecordScope``; the
    bot project re-adds it via ``BotRecordScope``. This test constructs a
    pool-scoped ``RecordScope`` for the audit store (matching the bot's
    canonical JSON) and uses this local subclass to avoid crossing the
    framework/examples boundary (mirrors ``BotRecordScope``).
    """

    pool: str | None = None


@pytest.fixture
async def connection(tmp_path: Path) -> AsyncIterator[ConnectionManager]:
    mgr = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await mgr.open()
    yield mgr
    await mgr.close()


@pytest.fixture
def codec_registry() -> RuntimeStateCodecRegistry:
    return RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})


@pytest.fixture
def turn_state_store(
    connection: ConnectionManager, codec_registry: RuntimeStateCodecRegistry
) -> SqliteTurnStateStore:
    return SqliteTurnStateStore(connection, codec_registry)


@pytest.fixture
def audit_store(
    connection: ConnectionManager,
) -> SqliteApprovalAuditStore:
    return SqliteApprovalAuditStore(connection, _PoolScopedRecordScope(pool="default"))


@pytest.fixture
def coordinator(
    connection: ConnectionManager, codec_registry: RuntimeStateCodecRegistry
) -> SqliteDecisionCoordinator:
    return SqliteDecisionCoordinator(connection, codec_registry)


def _make_snapshot(
    *,
    agent_id: str = "main",
    session_id: str = "s1.main",
    turn_id: str = "t1",
    turn_uuid: str = "uuid-1",
    phase: TurnPhase = TurnPhase.COMPLETED,
    reason: SnapshotReason = SnapshotReason.LLM_COMPLETED,
) -> TurnSnapshot:
    identity = TurnIdentity(
        agent_id=agent_id,
        session=SessionInfo.from_str(session_id),
        turn_id=turn_id,
    )
    state_payload: dict[str, JsonValue] = {
        "current_node": "tool",
        "iteration": 1,
        "tool_batches": [],
        "custom": {TurnCustomKey.TURN_UUID.value: turn_uuid},
    }
    return TurnSnapshot(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=phase,
        reason=reason,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=phase),
        message_delta=[],
        state_payload=state_payload,
    )


def _make_entry(
    *,
    turn_uuid: str = "uuid-1",
    session_id: str = "s1.main",
    agent_id: str = "main",
    turn_id: str = "t1",
    decision: ApprovalAuditDecision = ApprovalAuditDecision.APPROVED,
    deny_reason: str | None = None,
    decided_at: str = "2026-01-15T10:30:00+00:00",
) -> ApprovalAuditEntry:
    return ApprovalAuditEntry(
        turn_uuid=turn_uuid,
        session_id=session_id,
        agent_id=agent_id,
        turn_id=turn_id,
        tool_name="write_file",
        tool_call_id="call1",
        decision=decision,
        deny_reason=deny_reason,
        decided_at="2026-01-15T10:30:00+00:00",
        decided_by="user",
    )


@pytest.mark.parametrize(
    ("entry_update", "expected_field"),
    [
        ({"session_id": "other.main"}, "session_id"),
        ({"agent_id": "other"}, "agent_id"),
        ({"turn_id": "other-turn"}, "turn_id"),
    ],
)
async def test_apply_decision_rejects_mismatched_identity_without_writes(
    coordinator: SqliteDecisionCoordinator,
    turn_state_store: SqliteTurnStateStore,
    audit_store: SqliteApprovalAuditStore,
    entry_update: dict[str, JsonValue],
    expected_field: str,
) -> None:
    snapshot = _make_snapshot()
    entry = _make_entry().model_copy(update=entry_update)

    with pytest.raises(ValueError, match=expected_field):
        await coordinator.apply_decision(snapshot, entry)

    assert await turn_state_store.load_turn(snapshot.identity) is None
    assert await audit_store.query("s1.main") == []


@pytest.mark.parametrize(
    ("state_payload_update", "remove_turn_uuid"),
    [
        ({}, True),
        ({"custom": {TurnCustomKey.TURN_UUID.value: 123}}, False),
        ({"custom": {TurnCustomKey.TURN_UUID.value: "other-uuid"}}, False),
    ],
)
async def test_apply_decision_rejects_invalid_persisted_turn_uuid_without_writes(
    coordinator: SqliteDecisionCoordinator,
    turn_state_store: SqliteTurnStateStore,
    audit_store: SqliteApprovalAuditStore,
    state_payload_update: dict[str, JsonValue],
    remove_turn_uuid: bool,
) -> None:
    snapshot = _make_snapshot()
    state_payload = dict(snapshot.state_payload)
    if remove_turn_uuid:
        custom = state_payload.get("custom")
        if isinstance(custom, dict):
            custom.pop(TurnCustomKey.TURN_UUID.value, None)
    state_payload.update(state_payload_update)
    invalid_snapshot = TurnSnapshot(
        identity=snapshot.identity,
        agent_kind=snapshot.agent_kind,
        phase=snapshot.phase,
        reason=snapshot.reason,
        resume_point=snapshot.resume_point,
        message_delta=snapshot.message_delta,
        state_payload=state_payload,
        schema_version=snapshot.schema_version,
        created_at=snapshot.created_at,
    )

    with pytest.raises(ValueError, match="turn_uuid"):
        await coordinator.apply_decision(invalid_snapshot, _make_entry())

    assert await turn_state_store.load_turn(snapshot.identity) is None
    assert await audit_store.query("s1.main") == []


# ---------------------------------------------------------------------------
# Both succeed
# ---------------------------------------------------------------------------


async def test_apply_decision_persists_both_snapshot_and_audit(
    coordinator: SqliteDecisionCoordinator,
    turn_state_store: SqliteTurnStateStore,
    audit_store: SqliteApprovalAuditStore,
) -> None:
    snapshot = _make_snapshot()
    entry = _make_entry()

    await coordinator.apply_decision(snapshot, entry)

    loaded = await turn_state_store.load_turn(snapshot.identity)
    assert loaded is not None
    assert loaded.phase == TurnPhase.COMPLETED

    entries = await audit_store.query("s1.main")
    assert len(entries) == 1
    assert entries[0].turn_uuid == "uuid-1"
    assert entries[0].decision == "approved"


# ---------------------------------------------------------------------------
# Both fail — audit INSERT fails, snapshot rolled back
# ---------------------------------------------------------------------------


async def test_audit_failure_rolls_back_snapshot(
    coordinator: SqliteDecisionCoordinator,
    turn_state_store: SqliteTurnStateStore,
    audit_store: SqliteApprovalAuditStore,
) -> None:
    """If the audit INSERT fails (CHECK constraint), the snapshot is NOT persisted."""
    snapshot = _make_snapshot()
    # "bogus" violates the DB CHECK constraint (decision IN ('approved','denied'))
    bad_entry = _make_entry().model_copy(update={"decision": "bogus"})

    with pytest.raises(sqlite3.IntegrityError):
        await coordinator.apply_decision(snapshot, bad_entry)

    assert await turn_state_store.load_turn(snapshot.identity) is None
    assert await audit_store.query("s1.main") == []


# ---------------------------------------------------------------------------
# Both fail — active turn conflict, audit not persisted
# ---------------------------------------------------------------------------


async def test_active_turn_conflict_rolls_back_audit(
    coordinator: SqliteDecisionCoordinator,
    turn_state_store: SqliteTurnStateStore,
    audit_store: SqliteApprovalAuditStore,
) -> None:
    """If saving the snapshot hits an active-turn conflict, no audit row is written."""
    # Pre-existing active turn for the same (agent_id, session_id) with a different turn_id
    existing = _make_snapshot(turn_id="t0", phase=TurnPhase.RUNNING)
    await turn_state_store.save_turn(existing)

    new_snapshot = _make_snapshot(turn_id="t1", phase=TurnPhase.RUNNING)
    entry = _make_entry(turn_id="t1")

    with pytest.raises(ActiveTurnConflictError):
        await coordinator.apply_decision(new_snapshot, entry)

    entries = await audit_store.query("s1.main")
    assert entries == []


# ---------------------------------------------------------------------------
# Denied decision with deny_reason
# ---------------------------------------------------------------------------


async def test_apply_decision_with_denial(
    coordinator: SqliteDecisionCoordinator,
    turn_state_store: SqliteTurnStateStore,
    audit_store: SqliteApprovalAuditStore,
) -> None:
    snapshot = _make_snapshot()
    entry = _make_entry(
        decision=ApprovalAuditDecision.DENIED,
        deny_reason="too dangerous",
    )

    await coordinator.apply_decision(snapshot, entry)

    entries = await audit_store.query("s1.main")
    assert len(entries) == 1
    assert entries[0].decision == "denied"
    assert entries[0].deny_reason == "too dangerous"


# ---------------------------------------------------------------------------
# Multiple atomic writes for the same session
# ---------------------------------------------------------------------------


async def test_multiple_decisions_same_session(
    coordinator: SqliteDecisionCoordinator,
    audit_store: SqliteApprovalAuditStore,
) -> None:
    for i in range(3):
        snapshot = _make_snapshot(
            turn_id=f"t{i}",
            turn_uuid=f"uuid-{i}",
            phase=TurnPhase.COMPLETED,
        )
        entry = _make_entry(
            turn_uuid=f"uuid-{i}",
            turn_id=f"t{i}",
            decided_at=f"2026-01-15T10:{i:02d}:00+00:00",
        )
        await coordinator.apply_decision(snapshot, entry)

    entries = await audit_store.query("s1.main")
    assert len(entries) == 3
    assert [e.turn_uuid for e in entries] == ["uuid-0", "uuid-1", "uuid-2"]


async def test_audit_survives_snapshot_deletion(
    coordinator: SqliteDecisionCoordinator,
    turn_state_store: SqliteTurnStateStore,
    audit_store: SqliteApprovalAuditStore,
) -> None:
    snapshot = _make_snapshot()
    await coordinator.apply_decision(snapshot, _make_entry())

    await turn_state_store.delete_turn(snapshot.identity)

    assert await turn_state_store.load_turn(snapshot.identity) is None
    entries = await audit_store.query("s1.main")
    assert [entry.turn_uuid for entry in entries] == ["uuid-1"]


# ---------------------------------------------------------------------------
# ADR-0029 §2 regression — coordinator must write int-ms timestamps
# ---------------------------------------------------------------------------


async def test_coordinator_timestamps_round_trip_as_epoch_ms(
    coordinator: SqliteDecisionCoordinator,
    turn_state_store: SqliteTurnStateStore,
    audit_store: SqliteApprovalAuditStore,
) -> None:
    """Coordinator-written rows must decode through the proper adapters.

    Float-seconds writes into the INTEGER ms columns decode to 1970; this
    asserts snapshot ``created_at`` and audit ``decided_at`` round-trip to
    the correct modern epoch.
    """
    snapshot = _make_snapshot()
    entry = _make_entry()

    await coordinator.apply_decision(snapshot, entry)

    loaded = await turn_state_store.load_turn(snapshot.identity)
    assert loaded is not None
    assert datetime.fromtimestamp(loaded.created_at, tz=UTC).year > 2020

    entries = await audit_store.query("s1.main")
    assert len(entries) == 1
    assert datetime.fromisoformat(entries[0].decided_at).year > 2020

    since_entries = await audit_store.query(
        "s1.main", since=datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert len(since_entries) == 1
