"""ApprovalAuditStore conformance — file + sqlite backends.

The framework ships only :class:`SqliteApprovalAuditStore`. There is no
file-backed ``ApprovalAuditStore`` in ``src/`` because audit logging is a
workspace-persistence concern that always goes through the SQLite
``ConnectionManager``. For conformance coverage, this file defines a minimal
in-test :class:`JsonFileApprovalAuditStore` that persists entries as a JSONL
file, and runs the same assertions against both backends.

Both backends implement the ABC contract: ``record`` (append-only insert) and
``query`` (filter by session_id, optional ``since`` timestamp, ``limit`` cap).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest

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


class JsonFileApprovalAuditStore(ApprovalAuditStore):
    """Minimal file-backed ApprovalAuditStore for conformance testing.

    Persists entries as JSONL at ``<root>/audit.jsonl``. Append-only: each
    ``record`` appends one line. ``query`` scans the file linearly.
    Not for production use — no indexing, no concurrency control.
    """

    def __init__(self, root: Path) -> None:
        self._path = root / "audit.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def record(self, entry: ApprovalAuditEntry) -> None:
        line = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def query(
        self,
        session_id: str,
        since: datetime | None = None,
        limit: int = 100,
        decided_by: DecisionActor | None = None,
        source: ApprovalAuditSource | None = None,
    ) -> list[ApprovalAuditEntry]:
        if not self._path.exists():
            return []
        since_epoch = since.timestamp() if since is not None else None
        results: list[ApprovalAuditEntry] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data["session_id"] != session_id:
                    continue
                if decided_by is not None and data["decided_by"] != decided_by:
                    continue
                if source is not None and data.get("source") != source:
                    continue
                if since_epoch is not None:
                    entry_epoch = datetime.fromisoformat(data["decided_at"]).timestamp()
                    if entry_epoch < since_epoch:
                        continue
                results.append(ApprovalAuditEntry.model_validate(data))
                if len(results) >= limit:
                    break
        return results


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


@pytest.fixture(params=["file", "sqlite"])
async def audit_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    scope: RecordScope,
) -> AsyncGenerator[ApprovalAuditStore]:
    """Parametrized ApprovalAuditStore — file (JSONL) or sqlite."""
    if request.param == "file":
        yield JsonFileApprovalAuditStore(tmp_path / "audit_file")
    else:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        yield SqliteApprovalAuditStore(mgr, scope)
        await mgr.close()


class TestApprovalAuditStoreConformance:
    """Same behavior on both backends."""

    async def test_query_empty_returns_empty(self, audit_store: ApprovalAuditStore) -> None:
        assert await audit_store.query("s1.main") == []

    async def test_record_then_query_roundtrip(self, audit_store: ApprovalAuditStore) -> None:
        entry = _entry()
        await audit_store.record(entry)
        results = await audit_store.query("s1.main")
        assert len(results) == 1
        got = results[0]
        assert got.turn_uuid == "uuid-1"
        assert got.session_id == "s1.main"
        assert got.decision == "approved"
        assert got.deny_reason is None

    async def test_record_same_entry_twice_creates_two_rows(
        self, audit_store: ApprovalAuditStore
    ) -> None:
        entry = _entry()
        await audit_store.record(entry)
        await audit_store.record(entry)
        results = await audit_store.query("s1.main")
        assert len(results) == 2

    async def test_query_filters_by_session(self, audit_store: ApprovalAuditStore) -> None:
        await audit_store.record(_entry(session_id="s1.main", turn_uuid="u1"))
        await audit_store.record(_entry(session_id="s2.main", turn_uuid="u2"))
        results = await audit_store.query("s1.main")
        assert len(results) == 1
        assert results[0].turn_uuid == "u1"

    async def test_query_with_since_filter(self, audit_store: ApprovalAuditStore) -> None:
        await audit_store.record(_entry(turn_uuid="u1", decided_at="2026-01-15T10:00:00+00:00"))
        await audit_store.record(_entry(turn_uuid="u2", decided_at="2026-01-15T11:00:00+00:00"))
        since = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        results = await audit_store.query("s1.main", since=since)
        assert len(results) == 1
        assert results[0].turn_uuid == "u2"

    async def test_query_with_limit(self, audit_store: ApprovalAuditStore) -> None:
        for i in range(5):
            await audit_store.record(
                _entry(
                    turn_uuid=f"u{i}",
                    decided_at=f"2026-01-15T1{i}:00:00+00:00",
                )
            )
        results = await audit_store.query("s1.main", limit=3)
        assert len(results) == 3

    async def test_query_filters_by_decided_by(
        self, audit_store: ApprovalAuditStore
    ) -> None:
        await audit_store.record(
            _entry(
                turn_uuid="u1",
                decision=ApprovalAuditDecision.DENIED,
                decided_by=DecisionActor.SANDBOX_GUARD,
            )
        )
        await audit_store.record(_entry(turn_uuid="u2", decided_by=DecisionActor.USER))
        results = await audit_store.query(
            "s1.main", decided_by=DecisionActor.SANDBOX_GUARD
        )
        assert [r.turn_uuid for r in results] == ["u1"]
        results_all = await audit_store.query("s1.main")
        assert len(results_all) == 2

    async def test_deny_reason_preserved(self, audit_store: ApprovalAuditStore) -> None:
        await audit_store.record(
            _entry(decision=ApprovalAuditDecision.DENIED, deny_reason="too risky")
        )
        results = await audit_store.query("s1.main")
        assert len(results) == 1
        assert results[0].decision is ApprovalAuditDecision.DENIED
        assert results[0].deny_reason == "too risky"

    async def test_escalated_round_trips(
        self, audit_store: ApprovalAuditStore
    ) -> None:
        await audit_store.record(
            _entry(
                decision=ApprovalAuditDecision.ESCALATED,
                decided_by=DecisionActor.SANDBOX_GUARD,
            )
        )
        results = await audit_store.query("s1.main")
        assert len(results) == 1
        assert results[0].decision is ApprovalAuditDecision.ESCALATED

    async def test_source_round_trips_and_filters(
        self, audit_store: ApprovalAuditStore
    ) -> None:
        await audit_store.record(
            _entry(turn_uuid="u1", source=ApprovalAuditSource.DELEGATION)
        )
        await audit_store.record(_entry(turn_uuid="u2"))
        results = await audit_store.query(
            "s1.main", source=ApprovalAuditSource.DELEGATION
        )
        assert [r.turn_uuid for r in results] == ["u1"]
        results_all = await audit_store.query("s1.main")
        assert len(results_all) == 2
