"""SQLite decision coordinator — atomic snapshot + audit-entry write.

When an approval decision is made, the pipeline must update the
:class:`~modex_agent.runtime.models.TurnSnapshot` **and** append an
:class:`~modex_agent.persistence.adapters.approval_audit_store.ApprovalAuditEntry`
in one database transaction. This coordinator owns that atomic write.

The two domain ABCs (:class:`~modex_agent.runtime.store.TurnStateStore` and
:class:`~modex_agent.persistence.adapters.approval_audit_store.ApprovalAuditStore`)
remain independently swappable — each can be used standalone with its own
transaction. The coordinator is the only place that spans both tables in a
single ``ConnectionManager.transaction()``, so transaction orchestration does
not leak SQL into the ABCs.

The coordinator replicates the minimal SQL from
:class:`~modex_agent.persistence.adapters.turn_state_store.SqliteTurnStateStore.save_turn`
and
:class:`~modex_agent.persistence.adapters.approval_audit_store.SqliteApprovalAuditStore.record`
because the SQLite adapter methods each open their own transaction and the
``ConnectionManager`` forbids nesting.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from modex_agent.core.scope import RecordScope
from modex_agent.runtime.approval_decision import (
    ApprovalAuditEntry,
    ApprovalDecisionCoordinator,
)
from modex_agent.runtime.enums import TurnCustomKey, TurnPhase
from modex_agent.runtime.models import JsonValue, TurnSnapshot
from modex_agent.runtime.store import ActiveTurnConflictError

if TYPE_CHECKING:
    from modex_agent.persistence.connection import ConnectionManager
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry

_ACTIVE_PHASES = frozenset({TurnPhase.RUNNING, TurnPhase.SUSPENDED})
_ACTIVE_PHASE_SQL = "'running', 'suspended'"


@dataclass(frozen=True, slots=True)
class DecisionIdentityMismatchError(ValueError):
    """Raised when an audit entry does not identify its authoritative snapshot."""

    field: str
    expected: str
    actual: JsonValue

    def __str__(self) -> str:
        return (
            f"approval decision {self.field} mismatch: "
            f"expected {self.expected!r}, got {self.actual!r}"
        )


def _phase_to_db(phase: TurnPhase) -> str:
    """Map TurnPhase to the DB phase column value (mirrors SqliteTurnStateStore)."""
    if phase is TurnPhase.FAILED:
        return "error"
    return phase.value


def _build_scope(snapshot: TurnSnapshot) -> str:
    """Build the scope JSON from the snapshot (mirrors SqliteTurnStateStore)."""
    session = snapshot.identity.session
    return RecordScope(
        session_id=str(session),
        agent_id=snapshot.identity.agent_id,
        session_prefix=session.session_id_prefix,
        parent_session_id=session.parent_session_id,
    ).canonical()


def _iso_to_epoch(iso_str: str) -> float:
    """Parse an ISO-8601 timestamp string to epoch seconds (float)."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


class SqliteDecisionCoordinator(ApprovalDecisionCoordinator):
    """Atomic writer: persists a TurnSnapshot and an audit entry in one transaction.

    Used by the approval decision handler to guarantee that the snapshot update
    and the audit log append either both succeed or both fail. The coordinator
    opens one ``ConnectionManager.transaction()`` and executes both INSERTs on
    the transaction handle — it does **not** call
    :meth:`~modex_agent.runtime.store.TurnStateStore.save_turn` or
    :meth:`~modex_agent.persistence.adapters.approval_audit_store.ApprovalAuditStore.record`
    because those methods manage their own transactions and the
    ``ConnectionManager`` forbids nesting.

    Args:
        connection: The workspace ``ConnectionManager`` shared with other
            adapters.
        codec_registry: Codec registry for encoding the ``TurnSnapshot`` to
            its JSON payload.
    """

    def __init__(
        self,
        connection: ConnectionManager,
        codec_registry: RuntimeStateCodecRegistry,
    ) -> None:
        self._connection = connection
        self._codec_registry = codec_registry

    async def apply_decision(
        self,
        snapshot: TurnSnapshot,
        entry: ApprovalAuditEntry,
    ) -> None:
        """Atomically save *snapshot* and append *entry* to the audit log.

        Both writes are committed together. If either fails the entire
        transaction is rolled back — neither the snapshot nor the audit row
        is persisted.

        Raises:
            DecisionIdentityMismatchError: If the audit identity does not match
                the snapshot identity or its persisted turn UUID.
            ActiveTurnConflictError: If *snapshot* is an active phase
                (running/suspended) and another active turn already exists
                for the same ``(agent_id, session_id)``.
            sqlite3.IntegrityError: If the audit entry violates a DB
                constraint (e.g. an invalid ``decision`` value).
        """
        agent_id = snapshot.identity.agent_id
        session_id = str(snapshot.identity.session)
        turn_id = snapshot.identity.turn_id
        persisted_turn_uuid = snapshot.state_payload.get(TurnCustomKey.TURN_UUID.value)
        identity_pairs: tuple[tuple[str, str, JsonValue], ...] = (
            ("session_id", session_id, entry.session_id),
            ("agent_id", agent_id, entry.agent_id),
            ("turn_id", turn_id, entry.turn_id),
        )
        for field, expected, actual in identity_pairs:
            if actual != expected:
                raise DecisionIdentityMismatchError(
                    field=field,
                    expected=expected,
                    actual=actual,
                )
        if not isinstance(persisted_turn_uuid, str) or persisted_turn_uuid != entry.turn_uuid:
            raise DecisionIdentityMismatchError(
                field="turn_uuid",
                expected=entry.turn_uuid,
                actual=persisted_turn_uuid,
            )

        codec = self._codec_registry.get(snapshot.agent_kind)
        payload = codec.encode_turn(snapshot)
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        scope_json = _build_scope(snapshot)
        db_phase = _phase_to_db(snapshot.phase)
        now = time.time()
        decided_at_epoch = _iso_to_epoch(entry.decided_at)

        async with self._connection.transaction() as tx:
            # Active-turn conflict pre-check (same logic as save_turn).
            if snapshot.phase in _ACTIVE_PHASES:
                existing = await tx.query_one(
                    "SELECT turn_id FROM turn_snapshots "
                    "WHERE agent_id = ? AND session_id = ? "
                    f"AND phase IN ({_ACTIVE_PHASE_SQL}) "
                    "AND turn_id != ?",
                    (agent_id, session_id, turn_id),
                )
                if existing is not None:
                    raise ActiveTurnConflictError(
                        f"Active turn already exists for agent={agent_id} "
                        f"session={session_id}: "
                        f"existing={existing['turn_id']}, new={turn_id}"
                    )

            # Snapshot upsert — IntegrityError here means unique-index conflict.
            try:
                await tx.execute(
                    "INSERT INTO turn_snapshots "
                    "(session_id, agent_id, turn_id, scope, agent_kind, phase, "
                    " reason, created_at, updated_at, schema_version, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id, agent_id, turn_id) DO UPDATE SET "
                    " scope = excluded.scope,"
                    " agent_kind = excluded.agent_kind,"
                    " phase = excluded.phase,"
                    " reason = excluded.reason,"
                    " created_at = excluded.created_at,"
                    " updated_at = excluded.updated_at,"
                    " schema_version = excluded.schema_version,"
                    " payload_json = excluded.payload_json",
                    (
                        session_id,
                        agent_id,
                        turn_id,
                        scope_json,
                        snapshot.agent_kind.value,
                        db_phase,
                        snapshot.reason.value,
                        snapshot.created_at,
                        now,
                        snapshot.schema_version,
                        payload_json,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ActiveTurnConflictError(
                    f"Active turn already exists for agent={agent_id} "
                    f"session={session_id}: new={turn_id}"
                ) from None

            # Audit entry append — IntegrityError here (e.g. CHECK constraint)
            # propagates and rolls back the snapshot upsert above.
            await tx.execute(
                "INSERT INTO approval_audit_log "
                "(turn_uuid, session_id, scope, agent_id, turn_id, tool_name, "
                " tool_call_id, decision, deny_reason, decided_at, decided_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.turn_uuid,
                    entry.session_id,
                    scope_json,
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
