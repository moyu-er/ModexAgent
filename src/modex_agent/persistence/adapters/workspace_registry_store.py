"""SQLite-backed workspace registry store (T23).

Async adapter over the registry ``ConnectionManager`` providing:

- Workspace record CRUD keyed by ``target_path`` (resolved absolute path):
  ``list_workspaces``, ``upsert_workspace``, ``delete_workspace``,
  ``get_workspace``, ``get_workspace_by_id``.
- ``session_workspace_map`` CRUD for session->workspace routing:
  ``set_session_workspace``, ``get_session_workspace``,
  ``delete_session_workspace``, ``list_session_prefixes``.

The registry DB schema (T07 ``migrations/registry/001_initial.sql``) defines
``workspaces`` (``target_path`` UNIQUE, ``workspace_id`` PK) and
``session_workspace_map`` (``session_prefix`` PK, ``workspace_id`` FK ON
DELETE CASCADE).

This adapter implements the shared async workspace-registry interface.
"""

from __future__ import annotations

import json
from pathlib import Path
from sqlite3 import Row
from typing import Final

from modex_agent.persistence.connection import ConnectionManager
from modex_agent.workspace.record import WorkspaceRecord
from modex_agent.workspace.registry import ScopeRegistryStore

#: Whitelist of valid ``order_by`` values mapped to their column names.
#: Interpolated into SQL only after whitelist lookup (never user input
#: directly), so ORDER BY is safe from injection.
_ORDER_BY_COLUMNS: Final[dict[str, str]] = {
    "last_active": "last_active",
    "created_at": "created_at",
}

_SELECT_COLUMNS: Final[str] = (
    "workspace_id, target_path, display_name, created_at, last_active, is_home, metadata_json"
)


def _resolve_target(target_path: str) -> str:
    """Canonicalize ``target_path`` to its resolved absolute form.

    Matches :class:`~modex_agent.workspace.store.GlobalWorkspaceStore` so both
    backends key on the same canonical path (required for conformance and so
    the ``target_path`` UNIQUE constraint is meaningful across callers).
    """
    return str(Path(target_path).resolve())


def _row_to_record(row: Row) -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=row["workspace_id"],
        target_path=row["target_path"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        last_active=row["last_active"],
        is_home=bool(row["is_home"]),
        metadata_json=json.loads(row["metadata_json"]),
    )


class SqliteScopeRegistryStore(ScopeRegistryStore):
    """Registry-DB-backed workspace registry + session->workspace map.

    Constructed with an already-open :class:`ConnectionManager` (owned by a
    :class:`~modex_agent.persistence.managers.registry.RegistryPersistenceManager`).
    All operations are serialized through the manager's operation lock.
    """

    def __init__(self, connection: ConnectionManager) -> None:
        self._connection = connection

    # ------------------------------------------------------------------
    # Workspace record CRUD
    # ------------------------------------------------------------------

    async def list_workspaces(
        self, order_by: str = "last_active", limit: int = 20
    ) -> list[WorkspaceRecord]:
        """Return known workspace records, sorted by ``order_by`` descending.

        ``order_by`` must be one of ``"last_active"`` (default) or
        ``"created_at"``. ``limit`` caps the result count (default 20).
        """
        column = _ORDER_BY_COLUMNS.get(order_by)
        if column is None:
            raise ValueError(
                f"Unknown order_by {order_by!r}; expected one of {sorted(_ORDER_BY_COLUMNS)}"
            )
        rows = await self._connection.query_all(
            f"SELECT {_SELECT_COLUMNS} FROM workspaces ORDER BY {column} DESC LIMIT ?",
            (limit,),
        )
        return [_row_to_record(row) for row in rows]

    async def upsert_workspace(self, record: WorkspaceRecord) -> None:
        """Insert or replace (in place) the record keyed by ``target_path``.

        Uses ``ON CONFLICT(target_path) DO UPDATE`` rather than
        ``INSERT OR REPLACE`` so the existing row is updated in place — this
        preserves ``session_workspace_map`` rows that reference the workspace
        (``INSERT OR REPLACE`` would DELETE the row, firing the FK's
        ``ON DELETE CASCADE`` and wiping the session map on every re-upsert).
        """
        target = _resolve_target(record.target_path)
        await self._connection.execute(
            f"INSERT INTO workspaces ({_SELECT_COLUMNS}) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_path) DO UPDATE SET "
            "display_name = excluded.display_name, "
            "last_active = excluded.last_active, "
            "is_home = excluded.is_home, "
            "metadata_json = excluded.metadata_json",
            (
                record.workspace_id,
                target,
                record.display_name,
                record.created_at,
                record.last_active,
                1 if record.is_home else 0,
                json.dumps(record.metadata_json, ensure_ascii=False),
            ),
        )

    async def delete_workspace(self, target_path: str) -> None:
        """Remove the record keyed by ``target_path`` (no-op if absent).

        Cascades to ``session_workspace_map`` via the FK's ``ON DELETE
        CASCADE``.
        """
        await self._connection.execute(
            "DELETE FROM workspaces WHERE target_path = ?",
            (_resolve_target(target_path),),
        )

    async def get_workspace(self, target_path: str) -> WorkspaceRecord | None:
        """Return the record for ``target_path``, or ``None`` if absent."""
        row = await self._connection.query_one(
            f"SELECT {_SELECT_COLUMNS} FROM workspaces WHERE target_path = ?",
            (_resolve_target(target_path),),
        )
        return _row_to_record(row) if row is not None else None

    async def get_workspace_by_id(self, workspace_id: str) -> WorkspaceRecord | None:
        """Return the record for ``workspace_id``, or ``None`` if absent.

        Completes the session->workspace routing flow: a caller resolves
        ``session_prefix`` -> ``workspace_id`` via
        :meth:`get_session_workspace`, then fetches the full record here.
        """
        row = await self._connection.query_one(
            f"SELECT {_SELECT_COLUMNS} FROM workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        return _row_to_record(row) if row is not None else None

    # ------------------------------------------------------------------
    # session_workspace_map CRUD
    # ------------------------------------------------------------------

    async def set_session_workspace(self, session_prefix: str, workspace_id: str) -> None:
        """Insert or replace the ``session_prefix`` -> ``workspace_id`` mapping.

        The FK on ``workspace_id`` requires the workspace to exist first;
        a dangling ``workspace_id`` raises ``sqlite3.IntegrityError``.
        """
        await self._connection.execute(
            "INSERT INTO session_workspace_map (session_prefix, workspace_id) "
            "VALUES (?, ?) "
            "ON CONFLICT(session_prefix) DO UPDATE SET "
            "workspace_id = excluded.workspace_id",
            (session_prefix, workspace_id),
        )

    async def get_session_workspace(self, session_prefix: str) -> str | None:
        """Return the ``workspace_id`` mapped to ``session_prefix``, or ``None``."""
        row = await self._connection.query_one(
            "SELECT workspace_id FROM session_workspace_map WHERE session_prefix = ?",
            (session_prefix,),
        )
        return row["workspace_id"] if row is not None else None

    async def delete_session_workspace(self, session_prefix: str) -> None:
        """Remove the mapping for ``session_prefix`` (no-op if absent)."""
        await self._connection.execute(
            "DELETE FROM session_workspace_map WHERE session_prefix = ?",
            (session_prefix,),
        )

    async def list_session_prefixes(self, workspace_id: str) -> list[str]:
        """Return all ``session_prefix`` values mapped to ``workspace_id``."""
        rows = await self._connection.query_all(
            "SELECT session_prefix FROM session_workspace_map WHERE workspace_id = ?",
            (workspace_id,),
        )
        return [row["session_prefix"] for row in rows]
