"""Tests for :class:`SqliteApprovalAuditStore` — record/query, append-only, isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.approval.constants import (
    ApprovalAuditDecision,
    ApprovalAuditSource,
    DecisionActor,
)
from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.approval_audit_store import (
    ApprovalAuditEntry,
    ApprovalAuditStore,
    SqliteApprovalAuditStore,
)


class _PoolScopedRecordScope(RecordScope):
    """Framework-test-local ``RecordScope`` subclass adding the pool dimension.

    Framework tests need to construct pool-scoped scope_keys (matching the
    bot's ``BotRecordScope`` canonical JSON). ``BotRecordScope`` lives in the
    examples layer and cannot be imported by framework tests (ADR-0028
    layering); this local subclass mirrors its ``pool`` field so the tests
    can construct compatible scope_keys without crossing the boundary.
    """

    pool: str | None = None


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteApprovalAuditStore]:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    scope = _PoolScopedRecordScope(pool="default")
    yield SqliteApprovalAuditStore(manager, scope)
    await manager.close()


def _entry(
    *,
    turn_uuid: str = "uuid-1",
    session_id: str = "s1.main",
    agent_id: str = "main",
    turn_id: str = "t1",
    tool_name: str = "write_file",
    tool_call_id: str = "call1",
    decision: ApprovalAuditDecision = ApprovalAuditDecision.APPROVED,
    deny_reason: str | None = None,
    decided_at: str = "2026-01-15T10:30:00+00:00",
    decided_by: DecisionActor = DecisionActor.USER,
    source: ApprovalAuditSource = ApprovalAuditSource.RUNTIME,
) -> ApprovalAuditEntry:
    return ApprovalAuditEntry(
        turn_uuid=turn_uuid,
        session_id=session_id,
        agent_id=agent_id,
        turn_id=turn_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        decision=decision,
        deny_reason=deny_reason,
        decided_at=decided_at,
        decided_by=decided_by,
        source=source,
    )


# ---------------------------------------------------------------------------
# record -> query roundtrip
# ---------------------------------------------------------------------------


async def test_record_then_query_roundtrip(store: SqliteApprovalAuditStore) -> None:
    entry = _entry()
    await store.record(entry)

    results = await store.query("s1.main")
    assert len(results) == 1
    got = results[0]
    assert got.turn_uuid == "uuid-1"
    assert got.session_id == "s1.main"
    assert got.agent_id == "main"
    assert got.turn_id == "t1"
    assert got.tool_name == "write_file"
    assert got.tool_call_id == "call1"
    assert got.decision is ApprovalAuditDecision.APPROVED
    assert got.deny_reason is None
    assert got.decided_at == "2026-01-15T10:30:00+00:00"
    assert got.decided_by is DecisionActor.USER


async def test_query_missing_session_returns_empty(
    store: SqliteApprovalAuditStore,
) -> None:
    assert await store.query("never") == []


# ---------------------------------------------------------------------------
# append-only (no update / no delete)
# ---------------------------------------------------------------------------


async def test_record_same_entry_twice_creates_two_rows(
    store: SqliteApprovalAuditStore,
) -> None:
    entry = _entry()
    await store.record(entry)
    await store.record(entry)

    results = await store.query("s1.main")
    assert len(results) == 2


async def test_abc_has_no_update_or_delete_method() -> None:
    assert not hasattr(ApprovalAuditStore, "update")
    assert not hasattr(ApprovalAuditStore, "delete")


# ---------------------------------------------------------------------------
# query by session
# ---------------------------------------------------------------------------


async def test_query_filters_by_session(
    store: SqliteApprovalAuditStore,
) -> None:
    await store.record(_entry(session_id="s1.main", turn_uuid="u1"))
    await store.record(_entry(session_id="s2.main", turn_uuid="u2"))

    results = await store.query("s1.main")
    assert len(results) == 1
    assert results[0].turn_uuid == "u1"


# ---------------------------------------------------------------------------
# query with since filter
# ---------------------------------------------------------------------------


async def test_query_with_since_filter(
    store: SqliteApprovalAuditStore,
) -> None:
    await store.record(_entry(turn_uuid="u1", decided_at="2026-01-15T10:00:00+00:00"))
    await store.record(_entry(turn_uuid="u2", decided_at="2026-01-15T11:00:00+00:00"))

    since = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
    results = await store.query("s1.main", since=since)
    assert len(results) == 1
    assert results[0].turn_uuid == "u2"


async def test_query_since_inclusive_boundary(
    store: SqliteApprovalAuditStore,
) -> None:
    await store.record(_entry(turn_uuid="u1", decided_at="2026-01-15T10:00:00+00:00"))

    since = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    results = await store.query("s1.main", since=since)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# query with limit
# ---------------------------------------------------------------------------


async def test_query_with_limit(store: SqliteApprovalAuditStore) -> None:
    for i in range(5):
        await store.record(
            _entry(
                turn_uuid=f"u{i}",
                decided_at=f"2026-01-15T10:0{i}:00+00:00",
            )
        )

    results = await store.query("s1.main", limit=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# deny_reason preserved
# ---------------------------------------------------------------------------


async def test_deny_reason_preserved(store: SqliteApprovalAuditStore) -> None:
    entry = _entry(decision=ApprovalAuditDecision.DENIED, deny_reason="too dangerous")
    await store.record(entry)

    results = await store.query("s1.main")
    assert len(results) == 1
    assert results[0].decision is ApprovalAuditDecision.DENIED
    assert results[0].deny_reason == "too dangerous"


# ---------------------------------------------------------------------------
# query with decided_by filter (unified-security Ticket 06)
# ---------------------------------------------------------------------------


async def test_query_filters_by_decided_by(
    store: SqliteApprovalAuditStore,
) -> None:
    await store.record(
        _entry(turn_uuid="u1", decision=ApprovalAuditDecision.DENIED, decided_by=DecisionActor.SANDBOX_GUARD)
    )
    await store.record(_entry(turn_uuid="u2", decided_by=DecisionActor.USER))
    await store.record(
        _entry(
            turn_uuid="u3",
            decision=ApprovalAuditDecision.DENIED,
            deny_reason="boundary",
            decided_by=DecisionActor.SANDBOX_GUARD,
        )
    )

    guard_rows = await store.query("s1.main", decided_by=DecisionActor.SANDBOX_GUARD)
    assert [r.turn_uuid for r in guard_rows] == ["u1", "u3"]
    assert all(r.decided_by is DecisionActor.SANDBOX_GUARD for r in guard_rows)
    assert guard_rows[1].deny_reason == "boundary"

    user_rows = await store.query("s1.main", decided_by=DecisionActor.USER)
    assert [r.turn_uuid for r in user_rows] == ["u2"]


async def test_query_decided_by_no_match_returns_empty(
    store: SqliteApprovalAuditStore,
) -> None:
    await store.record(_entry(decided_by=DecisionActor.USER))
    assert await store.query("s1.main", decided_by=DecisionActor.SANDBOX_GUARD) == []


# ---------------------------------------------------------------------------
# ordering (ascending by decided_at)
# ---------------------------------------------------------------------------


async def test_query_returns_ascending_by_decided_at(
    store: SqliteApprovalAuditStore,
) -> None:
    await store.record(_entry(turn_uuid="u2", decided_at="2026-01-15T11:00:00+00:00"))
    await store.record(_entry(turn_uuid="u1", decided_at="2026-01-15T10:00:00+00:00"))
    await store.record(_entry(turn_uuid="u3", decided_at="2026-01-15T12:00:00+00:00"))

    results = await store.query("s1.main")
    assert [r.turn_uuid for r in results] == ["u1", "u2", "u3"]


# ---------------------------------------------------------------------------
# frozen model
# ---------------------------------------------------------------------------


def test_entry_is_frozen() -> None:
    entry = _entry()
    with pytest.raises(ValidationError):
        entry.decision = "denied"  # type: ignore[misc]
