"""`DispatchStore` — persistence abstraction for dispatch events (Task 09).

Provides:

- `DispatchStore` ABC (rule 7: ABC, not Protocol) — the minimal interface for
  recording and querying `DispatchEvent`s keyed by `run_id`.
- `InMemoryDispatchStore` — default in-memory dict implementation.
- `SqliteDispatchStore` — SQLite adapter using stdlib `sqlite3`. Lightweight
  `CREATE TABLE IF NOT EXISTS` migration (no dependency on modex_agent's
  `MigrationRunner`). Table/column names are module-level constants; all
  queries use `?` parameter placeholders (no string interpolation, no SQL
  injection surface).

Per ADR-0029: timestamps are epoch milliseconds. `modex_graph` cannot import
`modex_agent.utils.time`, so an equivalent `now_ms()` is defined here.

Per Task 09: `ParallelScheduler` accepts a `DispatchStore | None`; `None`
defaults to `InMemoryDispatchStore`. The scheduler generates a fresh `run_id`
per `run_async` call and records every `ctx.dispatch` event via
`dispatch_store.record(event, run_id)`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from abc import ABC, abstractmethod
from typing import Any

from .result import DispatchEvent

# ── Table / column name constants ─────────────────────────────────────────
# Centralized (rule 14) to avoid hardcoding in SQL strings. The DDL/DML
# statements below are assembled from these constants; all data values go
# through `?` parameter placeholders.

_DISPATCH_TABLE = "dispatch_events"
_COL_ID = "id"
_COL_RUN_ID = "run_id"
_COL_SOURCE_INSTANCE = "source_instance"
_COL_TARGET = "target"
_COL_PAYLOAD = "payload"
_COL_CREATED_AT_MS = "created_at_ms"


def now_ms() -> int:
    """Return current epoch time in milliseconds (ADR-0029).

    `modex_graph` cannot import `modex_agent.utils.time`, so this is the
    equivalent single source of truth for dispatch-store timestamps.
    """
    return int(time.time() * 1000)


class DispatchStore(ABC):
    """Persistence abstraction for `DispatchEvent` records (rule 7: ABC).

    The store is keyed by `run_id` — a string identifying one graph run (one
    `GraphEngine.run_async` call). Events from different runs are isolated.

    All methods are synchronous. The dispatch handler in `ParallelScheduler`
    runs synchronously (called from `GraphContext.dispatch` inside node
    `execute`), so a sync store matches the call site.

    Implementations:

    - `InMemoryDispatchStore` — dict-backed, default.
    - `SqliteDispatchStore` — SQLite file or `:memory:`.
    """

    @abstractmethod
    def record(self, event: DispatchEvent, run_id: str) -> None:
        """Persist a single `DispatchEvent` under `run_id`."""
        ...

    @abstractmethod
    def query_by_target(self, target: str, run_id: str) -> list[DispatchEvent]:
        """Return all events for `target` under `run_id`, in record order."""
        ...

    @abstractmethod
    def query_by_source(self, source_instance: str, run_id: str) -> list[DispatchEvent]:
        """Return all events from `source_instance` under `run_id`, in record order."""
        ...

    @abstractmethod
    def query_all(self, run_id: str) -> list[DispatchEvent]:
        """Return all events under `run_id`, in record order."""
        ...

    @abstractmethod
    def clear(self, run_id: str) -> None:
        """Delete all events under `run_id`."""
        ...


class InMemoryDispatchStore(DispatchStore):
    """Default in-memory `DispatchStore` — dict keyed by `run_id`.

    Events are stored in insertion order (Python list order is preserved).
    Suitable for single-process runs and tests. Not persistent across
    process restarts.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[DispatchEvent]] = {}

    def record(self, event: DispatchEvent, run_id: str) -> None:
        self._events.setdefault(run_id, []).append(event)

    def query_by_target(self, target: str, run_id: str) -> list[DispatchEvent]:
        return [e for e in self._events.get(run_id, []) if e.target == target]

    def query_by_source(self, source_instance: str, run_id: str) -> list[DispatchEvent]:
        return [e for e in self._events.get(run_id, []) if e.source_instance == source_instance]

    def query_all(self, run_id: str) -> list[DispatchEvent]:
        return list(self._events.get(run_id, []))

    def clear(self, run_id: str) -> None:
        self._events.pop(run_id, None)


class SqliteDispatchStore(DispatchStore):
    """SQLite-backed `DispatchStore` using stdlib `sqlite3`.

    Schema is created on construction via `CREATE TABLE IF NOT EXISTS`
    (lightweight migration — does not depend on modex_agent's
    `MigrationRunner`). Table and column names are module-level constants;
    all data values go through `?` parameter placeholders (no string
    interpolation, no SQL injection surface).

    The `payload` dict is serialized to JSON text on write and deserialized
    via `json.loads` on read. `None` payload is stored as SQL `NULL`.

    Timestamps are epoch milliseconds (`now_ms()`), per ADR-0029.

    The store holds a single `sqlite3.Connection` for its lifetime.
    `check_same_thread=False` allows the connection to be used from the
    event-loop thread or a thread-pool worker (the scheduler's sync `run()`
    path). Access is serialized by the GIL and the scheduler's synchronous
    dispatch handler — no concurrent writes.

    For `:memory:` databases, the schema and data live as long as the store
    instance. For file paths, data persists across process restarts.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the dispatch_events table + indexes if they don't exist."""
        conn = self._conn
        # DDL assembled from constants — no hardcoded table/column names.
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_DISPATCH_TABLE} ("
            f"{_COL_ID} INTEGER PRIMARY KEY AUTOINCREMENT, "
            f"{_COL_RUN_ID} TEXT NOT NULL, "
            f"{_COL_SOURCE_INSTANCE} TEXT NOT NULL, "
            f"{_COL_TARGET} TEXT NOT NULL, "
            f"{_COL_PAYLOAD} TEXT, "
            f"{_COL_CREATED_AT_MS} INTEGER NOT NULL"
            f")"
        )
        # Indexes covering the three query patterns.
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_DISPATCH_TABLE}_run_id "
            f"ON {_DISPATCH_TABLE} ({_COL_RUN_ID})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_DISPATCH_TABLE}_run_target "
            f"ON {_DISPATCH_TABLE} ({_COL_RUN_ID}, {_COL_TARGET})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_DISPATCH_TABLE}_run_source "
            f"ON {_DISPATCH_TABLE} ({_COL_RUN_ID}, {_COL_SOURCE_INSTANCE})"
        )
        conn.commit()

    def record(self, event: DispatchEvent, run_id: str) -> None:
        payload_text: str | None = json.dumps(event.payload) if event.payload is not None else None
        self._conn.execute(
            f"INSERT INTO {_DISPATCH_TABLE} "
            f"({_COL_RUN_ID}, {_COL_SOURCE_INSTANCE}, {_COL_TARGET}, "
            f"{_COL_PAYLOAD}, {_COL_CREATED_AT_MS}) "
            f"VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                event.source_instance,
                event.target,
                payload_text,
                now_ms(),
            ),
        )
        self._conn.commit()

    def query_by_target(self, target: str, run_id: str) -> list[DispatchEvent]:
        rows = self._conn.execute(
            f"SELECT {_COL_SOURCE_INSTANCE}, {_COL_TARGET}, {_COL_PAYLOAD} "
            f"FROM {_DISPATCH_TABLE} "
            f"WHERE {_COL_RUN_ID} = ? AND {_COL_TARGET} = ? "
            f"ORDER BY {_COL_ID}",
            (run_id, target),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def query_by_source(self, source_instance: str, run_id: str) -> list[DispatchEvent]:
        rows = self._conn.execute(
            f"SELECT {_COL_SOURCE_INSTANCE}, {_COL_TARGET}, {_COL_PAYLOAD} "
            f"FROM {_DISPATCH_TABLE} "
            f"WHERE {_COL_RUN_ID} = ? AND {_COL_SOURCE_INSTANCE} = ? "
            f"ORDER BY {_COL_ID}",
            (run_id, source_instance),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def query_all(self, run_id: str) -> list[DispatchEvent]:
        rows = self._conn.execute(
            f"SELECT {_COL_SOURCE_INSTANCE}, {_COL_TARGET}, {_COL_PAYLOAD} "
            f"FROM {_DISPATCH_TABLE} "
            f"WHERE {_COL_RUN_ID} = ? "
            f"ORDER BY {_COL_ID}",
            (run_id,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def clear(self, run_id: str) -> None:
        self._conn.execute(
            f"DELETE FROM {_DISPATCH_TABLE} WHERE {_COL_RUN_ID} = ?",
            (run_id,),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_event(row: tuple[Any, ...]) -> DispatchEvent:
        source_instance, target, payload_text = row
        payload = json.loads(payload_text) if payload_text is not None else None
        return DispatchEvent(
            source_instance=source_instance,
            target=target,
            payload=payload,
        )

    def close(self) -> None:
        """Close the underlying SQLite connection.

        Not part of the `DispatchStore` ABC — concrete resource cleanup for
        the SQLite adapter. Safe to call multiple times.
        """
        self._conn.close()


__all__ = [
    "DispatchStore",
    "InMemoryDispatchStore",
    "SqliteDispatchStore",
    "now_ms",
]
