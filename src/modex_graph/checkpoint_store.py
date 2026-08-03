# ruff: noqa: ANN401

"""`CheckpointStore` — persistence abstraction for scheduler checkpoints (D19).

Provides:

- `CheckpointStore` ABC (rule 7: ABC, not Protocol) — the minimal async
  interface for saving and loading `CheckpointData` keyed by `run_id`.
- `MemoryCheckpointStore` — default in-memory dict implementation.
- `SqliteCheckpointStore` — SQLite adapter using stdlib `sqlite3`. Stores
  each checkpoint as a single JSON blob in one row per save (the data is
  not queried by columns, only loaded wholesale). Table/column names are
  module-level constants; all queries use `?` parameter placeholders (no
  string interpolation, no SQL injection surface).

Per ADR-0034 D19: checkpoint timing is after each instance merge; the
scheduler schedules an async checkpoint. `ConcurrentWriteTracker` state is
NOT persisted. All methods are `async` so checkpoint save/load does not
block the event loop (implementations may delegate to a thread pool or
`aiosqlite` in the future; the stdlib `sqlite3` adapter here performs the
sync DB calls inline — the JSON blob is small).

`now_ms()` is imported from `dispatch_store.py` (the single source of truth
for `modex_graph` timestamps, per ADR-0029).
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .constants import NodeInstanceStatus
from .dispatch_store import now_ms
from .result import DispatchEvent

# ── Table / column name constants ─────────────────────────────────────────
# Centralized (rule 14) to avoid hardcoding in SQL strings. The DDL/DML
# statements below are assembled from these constants; all data values go
# through `?` parameter placeholders.

_CHECKPOINT_TABLE = "scheduler_checkpoints"
_COL_RUN_ID = "run_id"
_COL_SEQ = "seq"
_COL_DATA = "data"
_COL_CREATED_AT_MS = "created_at_ms"


class InstanceRecord(BaseModel):
    """Frozen record of one completed scheduler instance (rule 12).

    Fields:

    - `instance_id: str` — the `NodeInstance.instance_id` (e.g. `"llm#0"`).
    - `node_name: str` — the node that ran for this instance.
    - `fork_version: int` — the fork version the instance executed against.
    - `status: NodeInstanceStatus` — the terminal status of the instance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str = Field(description="The NodeInstance.instance_id (e.g. 'llm#0').")
    node_name: str = Field(description="The node that ran for this instance.")
    fork_version: int = Field(description="The fork version the instance executed against.")
    status: NodeInstanceStatus = Field(description="The terminal status of the instance.")


class CheckpointData(BaseModel):
    """Frozen snapshot of scheduler state for crash recovery (rule 12).

    Captured after each instance merge (ADR-0034 D19). The scheduler
    serializes this via `CheckpointStore.save` and restores it via
    `CheckpointStore.load_latest` on restart.

    `ConcurrentWriteTracker` state is intentionally NOT included — it is
    ephemeral per-generation bookkeeping that is rebuilt on resume.

    Fields:

    - `main_state: dict[str, Any]` — the output of `GraphState.checkpoint()`,
      a plain-dict snapshot of the main state channels.
    - `pending_on_all_preds: dict[str, dict[str, list[dict[str, Any] | None]]]`
      — the pending dispatch queues keyed by `(target_node, source_instance)`.
      Each entry is a list of payload dicts (or `None` payloads) awaiting
      consumption.
    - `completed_instances: list[InstanceRecord]` — the instances that have
      already completed; used to skip re-execution on resume.
    - `dispatch_events: list[DispatchEvent]` — the full dispatch audit log
      for this run (from `ParallelScheduler.dispatch_log`).
    - `graph_instance_id: int | None` — the graph instance this checkpoint
      belongs to (ticket 10 class 1). Replaces `run_id` as the persistence
      key (rule 15: converge on a single key). `None` means not yet
      assigned — backward compatible with existing construction that does
      not set it.
    - `activated_sources: dict[str, list[str]]` — ticket 10 class 1.
      `target_node → list of activated source node names`. Tracks which
      predecessors have actually dispatched to each target under
      `ParallelScheduler` (`NodeTrigger.ON_ALL_PREDS`).
    - `instance_seq: int` — ticket 10 class 1. Next instance sequence
      number, used for `NodeInstance.instance_id` generation
      (e.g. `"llm#3"` ← `instance_seq=3`).
    - `iteration_count: int` — ticket 10 class 1. Total iterations
      completed, for `GraphSpec.max_iterations` tracking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    main_state: dict[str, Any] = Field(
        description="Output of GraphState.checkpoint() — main state channel snapshot.",
    )
    pending_on_all_preds: dict[str, dict[str, list[dict[str, Any] | None]]] = Field(
        description=(
            "Pending dispatch queues keyed by (target_node, source_instance). "
            "Each entry is a list of payload dicts (or None) awaiting consumption."
        ),
    )
    completed_instances: list[InstanceRecord] = Field(
        default_factory=list,
        description="Instances that have already completed; used to skip re-execution on resume.",
    )
    dispatch_events: list[DispatchEvent] = Field(
        default_factory=list,
        description="The full dispatch audit log for this run.",
    )
    # ── Ticket 10 class 1 — graph-instance-keyed fields ────────────────
    graph_instance_id: int | None = Field(
        default=None,
        description=(
            "The graph instance this checkpoint belongs to (ticket 10 "
            "class 1). Snowflake ID — replaces run_id as the persistence "
            "key (rule 15: converge). None = not yet assigned, backward "
            "compatible with existing construction."
        ),
    )
    activated_sources: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Ticket 10 class 1. target_node → list of activated source "
            "node names. Tracks which predecessors have actually dispatched "
            "to each target under ParallelScheduler (ON_ALL_PREDS)."
        ),
    )
    instance_seq: int = Field(
        default=0,
        description=(
            "Ticket 10 class 1. Next instance sequence number, used for "
            "NodeInstance.instance_id generation (e.g. 'llm#3' ← 3)."
        ),
    )
    iteration_count: int = Field(
        default=0,
        description=(
            "Ticket 10 class 1. Total iterations completed, for GraphSpec.max_iterations tracking."
        ),
    )


class CheckpointStore(ABC):
    """Async persistence abstraction for `CheckpointData` (rule 7: ABC).

    The store is keyed by `run_id` — a string identifying one graph run
    (one scheduler `run_async` call). Checkpoints from different runs are
    isolated.

    All methods are `async` so checkpoint save/load does not block the
    event loop. The scheduler schedules an async checkpoint after each
    instance merge (ADR-0034 D19).

    Implementations:

    - `MemoryCheckpointStore` — dict-backed, default.
    - `SqliteCheckpointStore` — SQLite file or `:memory:`.
    """

    @abstractmethod
    async def save(self, data: CheckpointData, run_id: str) -> None:
        """Persist a `CheckpointData` snapshot under `run_id`.

        Each call appends a new checkpoint (monotonically increasing seq);
        `load_latest` returns the most recent.
        """
        ...

    @abstractmethod
    async def load_latest(self, run_id: str) -> CheckpointData | None:
        """Return the most recent `CheckpointData` for `run_id`, or `None`.

        `None` means no checkpoint exists for this run (fresh start).
        """
        ...

    @abstractmethod
    async def clear(self, run_id: str) -> None:
        """Delete all checkpoints under `run_id`."""
        ...


class MemoryCheckpointStore(CheckpointStore):
    """Default in-memory `CheckpointStore` — list keyed by `run_id`.

    Checkpoints are stored in insertion order (Python list order is
    preserved). `load_latest` returns the last appended entry. Suitable for
    single-process runs and tests. Not persistent across process restarts.
    """

    def __init__(self) -> None:
        self._checkpoints: dict[str, list[CheckpointData]] = {}

    async def save(self, data: CheckpointData, run_id: str) -> None:
        self._checkpoints.setdefault(run_id, []).append(data)

    async def load_latest(self, run_id: str) -> CheckpointData | None:
        entries = self._checkpoints.get(run_id, [])
        if not entries:
            return None
        return entries[-1]

    async def clear(self, run_id: str) -> None:
        self._checkpoints.pop(run_id, None)


class SqliteCheckpointStore(CheckpointStore):
    """SQLite-backed `CheckpointStore` using stdlib `sqlite3`.

    Each checkpoint is stored as a single JSON blob in one row per save
    (the data is not queried by columns, only loaded wholesale). The
    `CheckpointData` is serialized via `model_dump()` → `json.dumps` on
    write and deserialized via `json.loads` → `CheckpointData.model_validate`
    on read.

    Schema is created on construction via `CREATE TABLE IF NOT EXISTS`
    (lightweight migration — does not depend on modex_agent's
    `MigrationRunner`). Table and column names are module-level constants;
    all data values go through `?` parameter placeholders (no string
    interpolation, no SQL injection surface).

    The `seq` column is a per-`run_id` monotonic counter assigned at insert
    time via `COALESCE(MAX(seq), -1) + 1`. `load_latest` returns the row
    with the max `seq` for the given `run_id`.

    Timestamps are epoch milliseconds (`now_ms()`), per ADR-0029.

    The store holds a single `sqlite3.Connection` for its lifetime.
    `check_same_thread=False` allows the connection to be used from the
    event-loop thread or a thread-pool worker. Access is serialized by the
    GIL and the scheduler's single-writer checkpoint path — no concurrent
    writes.

    For `:memory:` databases, the schema and data live as long as the store
    instance. For file paths, data persists across process restarts.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the scheduler_checkpoints table + index if they don't exist."""
        conn = self._conn
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_CHECKPOINT_TABLE} ("
            f"{_COL_RUN_ID} TEXT NOT NULL, "
            f"{_COL_SEQ} INTEGER NOT NULL, "
            f"{_COL_DATA} TEXT NOT NULL, "
            f"{_COL_CREATED_AT_MS} INTEGER NOT NULL, "
            f"PRIMARY KEY ({_COL_RUN_ID}, {_COL_SEQ})"
            f")"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_CHECKPOINT_TABLE}_run_seq "
            f"ON {_CHECKPOINT_TABLE} ({_COL_RUN_ID}, {_COL_SEQ} DESC)"
        )
        conn.commit()

    async def save(self, data: CheckpointData, run_id: str) -> None:
        data_text = data.model_dump_json()
        self._conn.execute(
            f"INSERT INTO {_CHECKPOINT_TABLE} "
            f"({_COL_RUN_ID}, {_COL_SEQ}, {_COL_DATA}, {_COL_CREATED_AT_MS}) "
            f"VALUES (?, "
            f"(SELECT COALESCE(MAX({_COL_SEQ}), -1) + 1 "
            f"FROM {_CHECKPOINT_TABLE} WHERE {_COL_RUN_ID} = ?), "
            f"?, ?)",
            (run_id, run_id, data_text, now_ms()),
        )
        self._conn.commit()

    async def load_latest(self, run_id: str) -> CheckpointData | None:
        row = self._conn.execute(
            f"SELECT {_COL_DATA} FROM {_CHECKPOINT_TABLE} "
            f"WHERE {_COL_RUN_ID} = ? "
            f"ORDER BY {_COL_SEQ} DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return CheckpointData.model_validate_json(row[0])

    async def clear(self, run_id: str) -> None:
        self._conn.execute(
            f"DELETE FROM {_CHECKPOINT_TABLE} WHERE {_COL_RUN_ID} = ?",
            (run_id,),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection.

        Not part of the `CheckpointStore` ABC — concrete resource cleanup for
        the SQLite adapter. Safe to call multiple times.
        """
        self._conn.close()


__all__ = [
    "CheckpointStore",
    "CheckpointData",
    "InstanceRecord",
    "MemoryCheckpointStore",
    "SqliteCheckpointStore",
]
