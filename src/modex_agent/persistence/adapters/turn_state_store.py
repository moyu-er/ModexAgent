"""SQLite-backed TurnStateStore adapter.

Stores full TurnSnapshot as a versioned JSON payload (``payload_json``) with
denormalized indexed columns (``agent_id``, ``session_id``, ``turn_id``,
``phase``, ``reason``). A partial unique index
``idx_turn_active_unique ON turn_snapshots(agent_id, session_id) WHERE phase IN
('running', 'suspended')`` enforces at most one active turn per
(agent_id, session_id) at the database level.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from modex_agent.core.scope import RecordScope
from modex_agent.persistence.connection import ConnectionManager, SqlParameter
from modex_agent.runtime.codec import RuntimeStateCodecRegistry
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import StateQueryScope, TurnIdentity, TurnSnapshot
from modex_agent.runtime.store import ActiveTurnConflictError, TurnStateStore
from modex_agent.utils.time import now_ms

_ACTIVE_PHASES = frozenset({TurnPhase.RUNNING, TurnPhase.SUSPENDED})

_ACTIVE_PHASE_SQL = "'running', 'suspended'"


def _phase_to_db(phase: TurnPhase) -> str:
    """Map TurnPhase to the DB phase column value.

    The schema CHECK constraint allows: running, suspended, completed,
    cancelled, error. TurnPhase.FAILED maps to 'error'; other supported
    phases map directly to their enum value.
    """
    if phase is TurnPhase.FAILED:
        return "error"
    return phase.value


class SqliteTurnStateStore(TurnStateStore):
    """SQLite-backed TurnStateStore using ConnectionManager + codec registry."""

    def __init__(
        self,
        connection: ConnectionManager,
        codec_registry: RuntimeStateCodecRegistry,
    ) -> None:
        self._connection = connection
        self._codec_registry = codec_registry

    # ---- TurnStateStore ABC ----

    async def save_turn(self, snapshot: TurnSnapshot) -> None:
        codec = self._codec_registry.get(snapshot.agent_kind)
        payload = codec.encode_turn(snapshot)
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        scope_json = self._build_scope(snapshot)
        db_phase = _phase_to_db(snapshot.phase)
        now = now_ms()
        agent_id = snapshot.identity.agent_id
        session_id = str(snapshot.identity.session)
        turn_id = snapshot.identity.turn_id
        created_at_ms = int(snapshot.created_at * 1000)

        try:
            async with self._connection.transaction() as tx:
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
                await tx.execute(
                    "INSERT INTO turn_snapshots "
                    "(session_id, agent_id, turn_id, scope_key, agent_kind, phase, "
                    " reason, created_at, updated_at, schema_version, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id, agent_id, turn_id) DO UPDATE SET "
                    " scope_key = excluded.scope_key,"
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
                        created_at_ms,
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

    async def load_turn(self, identity: TurnIdentity) -> TurnSnapshot | None:
        row = await self._connection.query_one(
            "SELECT payload_json, agent_kind, created_at FROM turn_snapshots "
            "WHERE session_id = ? AND agent_id = ? AND turn_id = ?",
            (str(identity.session), identity.agent_id, identity.turn_id),
        )
        if row is None:
            return None
        return self._decode(row["payload_json"], row["agent_kind"], row["created_at"])

    async def delete_turn(self, identity: TurnIdentity) -> None:
        await self._connection.execute(
            "DELETE FROM turn_snapshots WHERE session_id = ? AND agent_id = ? AND turn_id = ?",
            (str(identity.session), identity.agent_id, identity.turn_id),
        )

    async def list_active_turns(self, scope: StateQueryScope) -> list[TurnSnapshot]:
        query = "SELECT payload_json, agent_kind, created_at FROM turn_snapshots WHERE 1=1"
        params: list[SqlParameter] = []
        if scope.agent_id is not None:
            query += " AND agent_id = ?"
            params.append(scope.agent_id)
        if scope.session_id is not None:
            query += " AND session_id = ?"
            params.append(scope.session_id)
        if scope.agent_kind is not None:
            query += " AND agent_kind = ?"
            params.append(scope.agent_kind.value)
        if scope.phase is not None:
            query += " AND phase = ?"
            params.append(_phase_to_db(scope.phase))
        if scope.reason is not None:
            query += " AND reason = ?"
            params.append(scope.reason.value)
        if scope.created_before is not None:
            query += " AND created_at < ?"
            params.append(int(scope.created_before * 1000))
        rows = await self._connection.query_all(query, params)
        return [
            self._decode(row["payload_json"], row["agent_kind"], row["created_at"])
            for row in rows
        ]

    # ---- Public helper (not part of ABC) ----

    async def find_active_turn(self, agent_id: str, session_id: str) -> TurnSnapshot | None:
        row = await self._connection.query_one(
            "SELECT payload_json, agent_kind, created_at FROM turn_snapshots "
            "WHERE agent_id = ? AND session_id = ? "
            f"AND phase IN ({_ACTIVE_PHASE_SQL}) "
            "LIMIT 1",
            (agent_id, session_id),
        )
        if row is None:
            return None
        return self._decode(row["payload_json"], row["agent_kind"], row["created_at"])

    # ---- Internal helpers ----

    @staticmethod
    def _build_scope(snapshot: TurnSnapshot) -> str:
        session = snapshot.identity.session
        return RecordScope(
            session_id=str(session),
            agent_id=snapshot.identity.agent_id,
            session_prefix=session.session_id_prefix,
            parent_session_id=session.parent_session_id,
        ).canonical()

    def _decode(
        self, payload_json: str, agent_kind_raw: str, created_at_ms: int
    ) -> TurnSnapshot:
        agent_kind = AgentKind(agent_kind_raw)
        codec = self._codec_registry.get(agent_kind)
        payload: dict[str, Any] = json.loads(payload_json)
        # Phase 1 boundary conversion: the runtime dataclass keeps
        # ``created_at`` as float seconds until Phase 2; the DB column is
        # int ms. Re-inject the canonical float-seconds value so the codec
        # decodes a payload consistent with what ``encode_turn`` produced.
        payload["created_at"] = created_at_ms / 1000.0
        return codec.decode_turn(payload)
