from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from pydantic import TypeAdapter

from ..constants import GraphInstanceStatus
from ._time import now_ms
from .graph_metadata import GraphInvocationContext, GraphMetadata

_INSTANCE_TABLE = "graph_instances"
_COL_GRAPH_INSTANCE_ID = "graph_instance_id"
_COL_SPEC_ID = "spec_id"
_COL_VERSION = "version"
_COL_PARENT_INSTANCE_ID = "parent_instance_id"
_COL_PARENT_NODE = "parent_node"
_COL_STATUS = "status"
_COL_NODE_ID_MAP_JSON = "node_id_map_json"
_COL_ATTRS_JSON = "attrs_json"
_COL_CREATED_AT = "created_at"
_COL_UPDATED_AT = "updated_at"

_NODE_ID_MAP_ADAPTER = TypeAdapter(dict[str, str])
_ATTRS_ADAPTER = TypeAdapter(dict[str, int | str | None])

_ALLOWED_STATUSES = frozenset(
    {"pending", "running", "paused", "stopped", "crashed", "completed", "failed"}
)

_SELECT_COLS = (
    f"{_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, {_COL_VERSION}, "
    f"{_COL_PARENT_INSTANCE_ID}, {_COL_PARENT_NODE}, {_COL_STATUS}, "
    f"{_COL_NODE_ID_MAP_JSON}, {_COL_ATTRS_JSON}, "
    f"{_COL_CREATED_AT}, {_COL_UPDATED_AT}"
)


class GraphInstanceStore(ABC):
    """Version-chain store for ``GraphMetadata`` records.

    One ``graph_instance_id`` per spec; each execution creates a new
    ``version`` row. ``load`` returns the latest version;
    ``load_by_status`` returns the latest version per instance matching
    the filter. Invocation lifecycle methods are isomorphic to
    ``NodeStateStore``.
    """

    @abstractmethod
    def save(self, metadata: GraphMetadata) -> None:
        ...

    @abstractmethod
    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        ...

    @abstractmethod
    def load_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]:
        ...

    @abstractmethod
    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]:
        ...

    @abstractmethod
    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        ...

    @abstractmethod
    def update_attrs(
        self, graph_instance_id: int, attrs: dict[str, int | str | None]
    ) -> None:
        """Merge supplied keys into the latest version's attrs.

        Prior versions are frozen as an audit trail. ``begin_invocation``
        copies the latest attrs into the new version before further updates.
        """
        ...

    @abstractmethod
    def delete(self, graph_instance_id: int) -> None:
        ...

    @abstractmethod
    def begin_invocation(self, graph_instance_id: int) -> GraphInvocationContext:
        ...

    @abstractmethod
    def complete_invocation(self, ctx: GraphInvocationContext) -> None:
        ...

    @abstractmethod
    def suspend_invocation(self, ctx: GraphInvocationContext) -> None:
        ...

    @abstractmethod
    def crash_invocation(self, ctx: GraphInvocationContext) -> None:
        ...

    @abstractmethod
    def finalize_invocation(self, ctx: GraphInvocationContext) -> None:
        ...


class NullGraphInstanceStore(GraphInstanceStore):
    """No-op graph instance store with no persistence or recovery support."""

    def save(self, metadata: GraphMetadata) -> None:
        pass

    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        return None

    def load_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]:
        return []

    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]:
        return []

    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        pass

    def update_attrs(
        self, graph_instance_id: int, attrs: dict[str, int | str | None]
    ) -> None:
        """Ignore attrs because this store persists no instance metadata."""
        pass

    def delete(self, graph_instance_id: int) -> None:
        pass

    def begin_invocation(self, graph_instance_id: int) -> GraphInvocationContext:
        return GraphInvocationContext(graph_instance_id=graph_instance_id, version=0)

    def complete_invocation(self, ctx: GraphInvocationContext) -> None:
        pass

    def suspend_invocation(self, ctx: GraphInvocationContext) -> None:
        pass

    def crash_invocation(self, ctx: GraphInvocationContext) -> None:
        pass

    def finalize_invocation(self, ctx: GraphInvocationContext) -> None:
        pass


class InMemoryGraphInstanceStore(GraphInstanceStore):
    def __init__(self) -> None:
        self._instances: dict[int, list[GraphMetadata]] = {}

    def save(self, metadata: GraphMetadata) -> None:
        self._instances.setdefault(metadata.graph_instance_id, []).append(metadata)

    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        versions = self._instances.get(graph_instance_id)
        if not versions:
            return None
        return versions[-1]

    def load_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]:
        return [
            v[-1] for v in self._instances.values() if v and v[-1].status == status
        ]

    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]:
        return [
            v[-1]
            for v in self._instances.values()
            if v and v[-1].parent_instance_id == parent_instance_id
        ]

    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        versions = self._instances.get(graph_instance_id)
        if not versions:
            return
        versions[-1] = versions[-1].model_copy(update={"status": status})

    def update_attrs(
        self, graph_instance_id: int, attrs: dict[str, int | str | None]
    ) -> None:
        versions = self._instances.get(graph_instance_id)
        if not versions:
            return
        merged_attrs = dict(versions[-1].attrs)
        merged_attrs.update(attrs)
        versions[-1] = versions[-1].model_copy(update={"attrs": merged_attrs})

    def delete(self, graph_instance_id: int) -> None:
        self._instances.pop(graph_instance_id, None)

    def begin_invocation(self, graph_instance_id: int) -> GraphInvocationContext:
        versions = self._instances.get(graph_instance_id)
        if not versions:
            raise ValueError(f"Graph instance {graph_instance_id} not found")
        latest = versions[-1]
        if latest.status == GraphInstanceStatus.RUNNING:
            versions[-1] = latest.model_copy(update={"status": GraphInstanceStatus.CRASHED})
        new_version = latest.version + 1
        versions.append(
            GraphMetadata(
                graph_instance_id=graph_instance_id,
                spec_id=latest.spec_id,
                version=new_version,
                parent_instance_id=latest.parent_instance_id,
                parent_node=latest.parent_node,
                status=GraphInstanceStatus.RUNNING,
                node_id_map=latest.node_id_map,
                attrs=dict(latest.attrs),
                created_at=now_ms(),
                updated_at=now_ms(),
            )
        )
        return GraphInvocationContext(graph_instance_id=graph_instance_id, version=new_version)

    def complete_invocation(self, ctx: GraphInvocationContext) -> None:
        self._cas(ctx, GraphInstanceStatus.RUNNING, GraphInstanceStatus.COMPLETED)

    def suspend_invocation(self, ctx: GraphInvocationContext) -> None:
        self._cas(ctx, GraphInstanceStatus.RUNNING, GraphInstanceStatus.PAUSED)

    def crash_invocation(self, ctx: GraphInvocationContext) -> None:
        self._cas(ctx, None, GraphInstanceStatus.CRASHED)

    def finalize_invocation(self, ctx: GraphInvocationContext) -> None:
        self._cas(ctx, GraphInstanceStatus.RUNNING, GraphInstanceStatus.CRASHED)

    def _cas(
        self, ctx: GraphInvocationContext, expected: GraphInstanceStatus | None, new: GraphInstanceStatus
    ) -> None:
        versions = self._instances.get(ctx.graph_instance_id)
        if not versions:
            return
        for i, m in enumerate(versions):
            if m.version == ctx.version:
                if expected is not None and m.status != expected:
                    return
                versions[i] = m.model_copy(update={"status": new})
                return


class SqliteGraphInstanceStore(GraphInstanceStore):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        conn = self._conn
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({_INSTANCE_TABLE})").fetchall()
        }
        if existing and (_COL_VERSION not in existing or _COL_NODE_ID_MAP_JSON not in existing):
            conn.execute(f"DROP TABLE IF EXISTS {_INSTANCE_TABLE}")
            existing = set()
        statuses = ", ".join(f"'{s}'" for s in sorted(_ALLOWED_STATUSES))
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_INSTANCE_TABLE} ("
            f"{_COL_GRAPH_INSTANCE_ID} INTEGER NOT NULL, "
            f"{_COL_SPEC_ID} INTEGER NOT NULL, "
            f"{_COL_VERSION} INTEGER NOT NULL DEFAULT 0, "
            f"{_COL_PARENT_INSTANCE_ID} INTEGER, "
            f"{_COL_PARENT_NODE} TEXT, "
            f"{_COL_STATUS} TEXT NOT NULL DEFAULT 'pending' "
            f"CHECK ({_COL_STATUS} IN ({statuses})), "
            f"{_COL_NODE_ID_MAP_JSON} TEXT NOT NULL DEFAULT '{{}}' "
            f"CHECK (json_valid({_COL_NODE_ID_MAP_JSON})), "
            f"{_COL_ATTRS_JSON} TEXT, "
            f"{_COL_CREATED_AT} INTEGER NOT NULL, "
            f"{_COL_UPDATED_AT} INTEGER NOT NULL, "
            f"PRIMARY KEY ({_COL_GRAPH_INSTANCE_ID}, {_COL_VERSION})"
            f")"
        )
        if existing and _COL_ATTRS_JSON not in existing:
            conn.execute(
                f"ALTER TABLE {_INSTANCE_TABLE} ADD COLUMN {_COL_ATTRS_JSON} TEXT"
            )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_INSTANCE_TABLE}_spec "
            f"ON {_INSTANCE_TABLE} ({_COL_SPEC_ID})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_INSTANCE_TABLE}_parent "
            f"ON {_INSTANCE_TABLE} ({_COL_PARENT_INSTANCE_ID}) "
            f"WHERE {_COL_PARENT_INSTANCE_ID} IS NOT NULL"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_INSTANCE_TABLE}_active "
            f"ON {_INSTANCE_TABLE} ({_COL_STATUS}) "
            f"WHERE {_COL_STATUS} IN ('running', 'paused', 'crashed')"
        )
        conn.commit()

    def save(self, metadata: GraphMetadata) -> None:
        ts = now_ms()
        self._conn.execute(
            f"INSERT INTO {_INSTANCE_TABLE} "
            f"({_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, {_COL_VERSION}, "
            f"{_COL_PARENT_INSTANCE_ID}, {_COL_PARENT_NODE}, "
            f"{_COL_STATUS}, {_COL_NODE_ID_MAP_JSON}, {_COL_ATTRS_JSON}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                metadata.graph_instance_id,
                metadata.spec_id,
                metadata.version,
                metadata.parent_instance_id,
                metadata.parent_node,
                metadata.status.value,
                _NODE_ID_MAP_ADAPTER.dump_json(metadata.node_id_map).decode("utf-8"),
                _ATTRS_ADAPTER.dump_json(metadata.attrs).decode("utf-8"),
                ts,
                ts,
            ),
        )
        self._conn.commit()

    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM {_INSTANCE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"ORDER BY {_COL_VERSION} DESC LIMIT 1",
            (graph_instance_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_metadata(row)

    def load_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]:
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM {_INSTANCE_TABLE} "
            f"WHERE ({_COL_GRAPH_INSTANCE_ID}, {_COL_VERSION}) IN ("
            f"  SELECT {_COL_GRAPH_INSTANCE_ID}, MAX({_COL_VERSION})"
            f"  FROM {_INSTANCE_TABLE} GROUP BY {_COL_GRAPH_INSTANCE_ID}"
            f") AND {_COL_STATUS} = ? "
            f"ORDER BY {_COL_GRAPH_INSTANCE_ID}",
            (status.value,),
        ).fetchall()
        return [self._row_to_metadata(r) for r in rows]

    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]:
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM {_INSTANCE_TABLE} "
            f"WHERE ({_COL_GRAPH_INSTANCE_ID}, {_COL_VERSION}) IN ("
            f"  SELECT {_COL_GRAPH_INSTANCE_ID}, MAX({_COL_VERSION})"
            f"  FROM {_INSTANCE_TABLE} GROUP BY {_COL_GRAPH_INSTANCE_ID}"
            f") AND {_COL_PARENT_INSTANCE_ID} = ? "
            f"ORDER BY {_COL_GRAPH_INSTANCE_ID}",
            (parent_instance_id,),
        ).fetchall()
        return [self._row_to_metadata(r) for r in rows]

    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        self._conn.execute(
            f"UPDATE {_INSTANCE_TABLE} "
            f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"AND {_COL_VERSION} = ("
            f"  SELECT MAX({_COL_VERSION}) FROM {_INSTANCE_TABLE}"
            f"  WHERE {_COL_GRAPH_INSTANCE_ID} = ?)",
            (status.value, now_ms(), graph_instance_id, graph_instance_id),
        )
        self._conn.commit()

    def update_attrs(
        self, graph_instance_id: int, attrs: dict[str, int | str | None]
    ) -> None:
        row = self._conn.execute(
            f"SELECT {_COL_ATTRS_JSON} FROM {_INSTANCE_TABLE} "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"AND {_COL_VERSION} = ("
            f"  SELECT MAX({_COL_VERSION}) FROM {_INSTANCE_TABLE}"
            f"  WHERE {_COL_GRAPH_INSTANCE_ID} = ?)",
            (graph_instance_id, graph_instance_id),
        ).fetchone()
        if row is None:
            return
        merged_attrs = (
            _ATTRS_ADAPTER.validate_json(row[0]) if row[0] is not None else {}
        )
        merged_attrs.update(attrs)
        self._conn.execute(
            f"UPDATE {_INSTANCE_TABLE} SET {_COL_ATTRS_JSON} = ? "
            f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? "
            f"AND {_COL_VERSION} = ("
            f"  SELECT MAX({_COL_VERSION}) FROM {_INSTANCE_TABLE}"
            f"  WHERE {_COL_GRAPH_INSTANCE_ID} = ?)",
            (
                _ATTRS_ADAPTER.dump_json(merged_attrs).decode("utf-8"),
                graph_instance_id,
                graph_instance_id,
            ),
        )
        self._conn.commit()

    def delete(self, graph_instance_id: int) -> None:
        self._conn.execute(
            f"DELETE FROM {_INSTANCE_TABLE} WHERE {_COL_GRAPH_INSTANCE_ID} = ?",
            (graph_instance_id,),
        )
        self._conn.commit()

    def begin_invocation(self, graph_instance_id: int) -> GraphInvocationContext:
        latest = self.load(graph_instance_id)
        if latest is None:
            raise ValueError(f"Graph instance {graph_instance_id} not found")
        if latest.status == GraphInstanceStatus.RUNNING:
            self._conn.execute(
                f"UPDATE {_INSTANCE_TABLE} SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
                f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_VERSION} = ?",
                (GraphInstanceStatus.CRASHED.value, now_ms(), graph_instance_id, latest.version),
            )
        new_version = latest.version + 1
        ts = now_ms()
        self._conn.execute(
            f"INSERT INTO {_INSTANCE_TABLE} "
            f"({_COL_GRAPH_INSTANCE_ID}, {_COL_SPEC_ID}, {_COL_VERSION}, "
            f"{_COL_PARENT_INSTANCE_ID}, {_COL_PARENT_NODE}, "
            f"{_COL_STATUS}, {_COL_NODE_ID_MAP_JSON}, {_COL_ATTRS_JSON}, "
            f"{_COL_CREATED_AT}, {_COL_UPDATED_AT}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                graph_instance_id,
                latest.spec_id,
                new_version,
                latest.parent_instance_id,
                latest.parent_node,
                GraphInstanceStatus.RUNNING.value,
                _NODE_ID_MAP_ADAPTER.dump_json(latest.node_id_map).decode("utf-8"),
                _ATTRS_ADAPTER.dump_json(latest.attrs).decode("utf-8"),
                ts,
                ts,
            ),
        )
        self._conn.commit()
        return GraphInvocationContext(graph_instance_id=graph_instance_id, version=new_version)

    def complete_invocation(self, ctx: GraphInvocationContext) -> None:
        self._cas(ctx, GraphInstanceStatus.RUNNING, GraphInstanceStatus.COMPLETED)

    def suspend_invocation(self, ctx: GraphInvocationContext) -> None:
        self._cas(ctx, GraphInstanceStatus.RUNNING, GraphInstanceStatus.PAUSED)

    def crash_invocation(self, ctx: GraphInvocationContext) -> None:
        self._cas(ctx, None, GraphInstanceStatus.CRASHED)

    def finalize_invocation(self, ctx: GraphInvocationContext) -> None:
        self._cas(ctx, GraphInstanceStatus.RUNNING, GraphInstanceStatus.CRASHED)

    def _cas(
        self, ctx: GraphInvocationContext, expected: GraphInstanceStatus | None, new: GraphInstanceStatus
    ) -> None:
        if expected is not None:
            self._conn.execute(
                f"UPDATE {_INSTANCE_TABLE} "
                f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
                f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_VERSION} = ? "
                f"AND {_COL_STATUS} = ?",
                (new.value, now_ms(), ctx.graph_instance_id, ctx.version, expected.value),
            )
        else:
            self._conn.execute(
                f"UPDATE {_INSTANCE_TABLE} "
                f"SET {_COL_STATUS} = ?, {_COL_UPDATED_AT} = ? "
                f"WHERE {_COL_GRAPH_INSTANCE_ID} = ? AND {_COL_VERSION} = ?",
                (new.value, now_ms(), ctx.graph_instance_id, ctx.version),
            )
        self._conn.commit()

    @staticmethod
    def _row_to_metadata(row: tuple[Any, ...]) -> GraphMetadata:
        (
            graph_instance_id, spec_id, version,
            parent_instance_id, parent_node, status,
            node_id_map_json, attrs_json, created_at, updated_at,
        ) = row
        return GraphMetadata(
            graph_instance_id=graph_instance_id,
            spec_id=spec_id,
            version=version,
            parent_instance_id=parent_instance_id,
            parent_node=parent_node,
            status=GraphInstanceStatus(status),
            node_id_map=_NODE_ID_MAP_ADAPTER.validate_json(node_id_map_json),
            attrs=(
                _ATTRS_ADAPTER.validate_json(attrs_json)
                if attrs_json is not None
                else {}
            ),
            created_at=created_at,
            updated_at=updated_at,
        )


__all__ = [
    "GraphInstanceStore",
    "InMemoryGraphInstanceStore",
    "NullGraphInstanceStore",
    "SqliteGraphInstanceStore",
]
