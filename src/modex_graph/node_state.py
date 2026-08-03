# ruff: noqa: ANN401

"""`NodeState` ABC + `SimpleNodeState` impl — per-node in-memory state (ticket 10).

Manages a node's PRIVATE internal state during execution. Distinct from:

- `GraphState` (graph-level, shared across nodes via `ctx.state`) — Pydantic
  state that flows through the graph as the shared "blackboard".
- `NodeStateStore` (persistence layer, SQLite CRUD for the `node_states`
  table) — the on-disk append-only MVCC store keyed by
  `(graph_instance_id, node_name, version)`.

`NodeState` is the IN-MEMORY abstraction a node uses to manage its own
internal state (e.g. `_pending_delivers`, per-execution scratch values,
accumulated tool outputs). Reads/writes hit an in-memory dict first.
Business implementations may optionally sync to `NodeStateStore` for
crash recovery — nodes choose whether to persist by providing a store +
`graph_instance_id` (sync wiring is a business-layer concern; the
framework default `SimpleNodeState` is in-memory only).

`NodeState` is a runtime object with mutable state, so it is NOT a
frozen Pydantic `BaseModel` (rule 12 exception — value-object rule
applies to cross-module structured data, not in-memory runtime caches).
It is an ABC (rule 7: ABC, not Protocol) with `Any` field values
(`# ruff: noqa: ANN401` — field values are node-defined and may be any
type the node chooses to stash, including non-Pydantic runtime objects
like `ToolResult` / `LLMResponse`).

Business implementations (`modex_agent`) may provide MVCC, multi-version,
or stateless variants. `SimpleNodeState` is the framework default — a
plain dict with no versioning, no conflict detection. Suitable for
`LinearScheduler` (single-path, no concurrent writes to the same node).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class NodeState(ABC):
    """In-memory per-node state abstraction (ticket 10).

    Manages a node's PRIVATE internal state during execution — distinct
    from `GraphState` (graph-level, shared) and `NodeStateStore`
    (persistence).

    In-memory cache first: reads/writes hit an in-memory dict. Optional
    sync to `NodeStateStore` for persistence (crash recovery) is a
    business-layer concern — nodes choose whether to persist by
    providing a store + `graph_instance_id`.

    Business implementations (`modex_agent`) may provide MVCC,
    multi-version, or stateless variants. `SimpleNodeState` is the
    framework default.

    Contract:

    - `read(field)`: return the stored value. Raises `KeyError` if not
      present (use `has()` to check first if uncertain).
    - `write(field, value)`: store a value (in-memory). Overwrites any
      existing value for the field.
    - `snapshot()`: return a shallow-copy dict of all state (for
      persistence/checkpoint). The caller may mutate the returned dict
      without affecting internal state.
    - `restore(data)`: replace ALL current state with the contents of
      `data`. Prior state is discarded.
    - `has(field)`: return `True` if a field exists (has been written
      or restored).
    """

    @abstractmethod
    def read(self, field: str) -> Any:
        """Read a field value.

        Args:
            field: The field name.

        Returns:
            The stored value (any type the node chose to stash).

        Raises:
            KeyError: If the field is not present. Use `has()` to check
                first if uncertain.
        """
        ...

    @abstractmethod
    def write(self, field: str, value: Any) -> None:
        """Write a field value (in-memory).

        Overwrites any existing value for the field.

        Args:
            field: The field name.
            value: The value (any type).
        """
        ...

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Snapshot all state as a dict (for persistence/checkpoint).

        Returns a shallow copy — the caller may mutate the returned dict
        without affecting internal state. Deep-copying nested mutable
        values is the caller's responsibility if needed (the framework
        default `SimpleNodeState.snapshot` returns a shallow copy, which
        is sufficient for the common case where field values are
        replaced rather than mutated in place).

        Returns:
            A dict mapping field names to their current values. Empty
            dict if no fields have been written.
        """
        ...

    @abstractmethod
    def restore(self, data: dict[str, Any]) -> None:
        """Restore state from a snapshot dict.

        Replaces ALL current state with the contents of `data`. Prior
        state is discarded. The provided dict is shallow-copied so the
        caller may safely mutate it after `restore` returns.

        Args:
            data: The snapshot dict to restore from.
        """
        ...

    @abstractmethod
    def has(self, field: str) -> bool:
        """Check if a field exists.

        Returns `True` if the field has been written (or restored from a
        snapshot that contained it), `False` otherwise.

        Args:
            field: The field name.

        Returns:
            `True` if the field is present, `False` otherwise.
        """
        ...


class SimpleNodeState(NodeState):
    """Single-state (no MVCC) dict-backed `NodeState`.

    The simplest `NodeState` — a plain dict with
    read/write/snapshot/restore/has. No versioning, no conflict
    detection. Suitable for `LinearScheduler` (single-path, no
    concurrent writes to the same node).

    For `ParallelScheduler` fan-out where the SAME `Node` instance is
    shared across concurrent executions, use a per-instance state wrapper
    or an MVCC implementation (business-layer concern — not provided
    here). Sharing a single `SimpleNodeState` across concurrent
    executions would race.
    """

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        """Initialize with optional initial state.

        Args:
            initial: Optional initial state dict. Shallow-copied so the
                caller may safely mutate the dict after construction.
                `None` (default) starts with an empty state.
        """
        # Shallow-copy initial to avoid aliasing the caller's dict.
        # Field values themselves are NOT deep-copied — nodes that stash
        # mutable values and share them externally should copy on write.
        self._data: dict[str, Any] = dict(initial) if initial else {}

    def read(self, field: str) -> Any:
        if field not in self._data:
            raise KeyError(
                f"NodeState field {field!r} not present. "
                f"Available fields: {sorted(self._data.keys())}"
            )
        return self._data[field]

    def write(self, field: str, value: Any) -> None:
        self._data[field] = value

    def snapshot(self) -> dict[str, Any]:
        # Shallow copy — caller may mutate the returned dict freely.
        # Nested mutable values are shared by reference; the common case
        # (replace-the-field writes) is safe. Deep-copying is the
        # caller's responsibility if they mutate-in-place stashed values.
        return dict(self._data)

    def restore(self, data: dict[str, Any]) -> None:
        # Replace ALL current state with a shallow copy of `data`.
        # Prior state is discarded.
        self._data = dict(data)

    def has(self, field: str) -> bool:
        return field in self._data


__all__ = ["NodeState", "SimpleNodeState"]
