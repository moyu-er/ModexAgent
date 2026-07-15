"""Approval audit store — ABC, entry model, and SQLite adapter.

Append-only audit log for approval decisions. Each :meth:`record` inserts one
row into the ``approval_audit_log`` table; no UPDATE or DELETE is exposed. The
store participates in workspace-persistence atomic writes via the
:class:`~modex_agent.persistence.coordinator.SqliteDecisionCoordinator`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from modex_agent.persistence.connection import SqlParameter
from modex_agent.runtime.approval_decision import ApprovalAuditEntry

if TYPE_CHECKING:
    from modex_agent.core.scope import RecordScope
    from modex_agent.persistence.connection import ConnectionManager


class ApprovalAuditStore(ABC):
    """Append-only audit log for approval decisions.

    The ABC defines two operations: :meth:`record` (append one entry) and
    :meth:`query` (filter by session and optional timestamp). Implementations
    MUST NOT expose update or delete — the log is append-only.
    """

    @abstractmethod
    async def record(self, entry: ApprovalAuditEntry) -> None:
        """Append *entry* to the audit log."""
        ...

    @abstractmethod
    async def query(
        self,
        session_id: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[ApprovalAuditEntry]:
        """Return audit entries for *session_id*, optionally filtered by *since*.

        Entries are returned in ascending ``decided_at`` order. When *since*
        is given, only entries at or after that moment are returned. *limit*
        caps the number of entries.
        """
        ...


# ---------------------------------------------------------------------------
# SQLite adapter
# ---------------------------------------------------------------------------


def _iso_to_epoch(iso_str: str) -> float:
    """Parse an ISO-8601 timestamp string to epoch seconds (float)."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _epoch_to_iso(epoch: float) -> str:
    """Convert epoch seconds (float) to an ISO-8601 UTC timestamp string."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


class SqliteApprovalAuditStore(ApprovalAuditStore):
    """SQLite-backed append-only approval audit log.

    Uses the ``approval_audit_log`` table. Each :meth:`record` is a single
    ``INSERT`` — no ``ON CONFLICT`` clause, so recording the same entry twice
    creates two rows. The ``scope`` column is populated from the injected
    :class:`RecordScope` so the generated ``pool`` column is available for
    workspace-level queries.

    Args:
        connection: The workspace ``ConnectionManager`` shared with other
            adapters.
        scope: A ``RecordScope`` whose canonical JSON populates the
            ``scope`` column.
    """

    def __init__(self, connection: ConnectionManager, scope: RecordScope) -> None:
        self._connection = connection
        self._scope_json = scope.canonical()

    async def record(self, entry: ApprovalAuditEntry) -> None:
        """Append *entry* as one row. Fails on DB constraint violations."""
        decided_at_epoch = _iso_to_epoch(entry.decided_at)
        await self._connection.execute(
            "INSERT INTO approval_audit_log "
            "(turn_uuid, session_id, scope, agent_id, turn_id, tool_name, "
            " tool_call_id, decision, deny_reason, decided_at, decided_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.turn_uuid,
                entry.session_id,
                self._scope_json,
                entry.agent_id,
                entry.turn_id,
                entry.tool_name,
                entry.tool_call_id,
                entry.decision,
                entry.deny_reason,
                decided_at_epoch,
                entry.decided_by,
            ),
        )

    async def query(
        self,
        session_id: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[ApprovalAuditEntry]:
        """Return entries for *session_id*, optionally filtered and capped."""
        query = (
            "SELECT turn_uuid, session_id, agent_id, turn_id, tool_name, "
            "tool_call_id, decision, deny_reason, decided_at, decided_by "
            "FROM approval_audit_log WHERE session_id = ?"
        )
        params: list[SqlParameter] = [session_id]
        if since is not None:
            since_epoch = since.timestamp()
            query += " AND decided_at >= ?"
            params.append(since_epoch)
        query += " ORDER BY decided_at ASC, id ASC LIMIT ?"
        params.append(limit)

        rows = await self._connection.query_all(query, params)
        return [
            ApprovalAuditEntry(
                turn_uuid=row["turn_uuid"],
                session_id=row["session_id"],
                agent_id=row["agent_id"],
                turn_id=row["turn_id"],
                tool_name=row["tool_name"],
                tool_call_id=row["tool_call_id"],
                decision=row["decision"],
                deny_reason=row["deny_reason"],
                decided_at=_epoch_to_iso(row["decided_at"]),
                decided_by=row["decided_by"],
            )
            for row in rows
        ]
