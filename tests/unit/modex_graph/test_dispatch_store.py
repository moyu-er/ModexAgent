"""Tests for DispatchStore ABC + InMemoryDispatchStore + SqliteDispatchStore.

Covers Task 09 acceptance criteria:

- `DispatchStore` ABC (rule 7: ABC, not Protocol) with 5 methods:
  record / query_by_target / query_by_source / query_all / clear.
- `InMemoryDispatchStore` default implementation (dict-backed).
- `SqliteDispatchStore` SQLite adapter: `CREATE TABLE IF NOT EXISTS`,
  parameterized queries, JSON payload serialization, epoch-ms timestamps.
- `ParallelScheduler` accepts `dispatch_store: DispatchStore | None = None`;
  `None` defaults to `InMemoryDispatchStore`.
- Multiple graph runs (different `run_id`) do not interfere.
- Table/column names managed via constants (no hardcoded SQL).
- Timestamps follow ADR-0029 (epoch ms).
- `now_ms()` defined in modex_graph (no modex_agent import).
"""

from __future__ import annotations

import tempfile
from abc import ABC
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from helpers import CounterState, make_runtime, make_coordinator

from modex_graph import (
    DispatchEvent,
    DispatchStore,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    InMemoryDispatchStore,
    IntegratedInput,
    Node,
    NodeResult,
    ParallelScheduler,
    SchedulerKind,
    SqliteDispatchStore,
)

# ── Test helpers ──────────────────────────────────────────────────────────


def _make_event(
    source: str = "a#0",
    target: str = "b",
    payload: dict[str, Any] | None = None,
) -> DispatchEvent:
    return DispatchEvent(source_instance=source, target=target, payload=payload)


def _store_factory(kind: str) -> Callable[[], DispatchStore]:
    """Return a factory that creates a fresh DispatchStore of the given kind."""
    if kind == "memory":
        return lambda: InMemoryDispatchStore()
    if kind == "sqlite":
        return lambda: SqliteDispatchStore(":memory:")
    raise ValueError(f"unknown kind: {kind}")


STORE_KINDS = ["memory", "sqlite"]


# ── DispatchStore ABC ─────────────────────────────────────────────────────


class TestDispatchStoreABC:
    def test_is_abc(self) -> None:
        assert issubclass(DispatchStore, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            DispatchStore()  # type: ignore[abstract]

    def test_five_abstract_methods(self) -> None:
        expected = {
            "record",
            "query_by_target",
            "query_by_source",
            "query_all",
            "clear",
        }
        assert set(DispatchStore.__abstractmethods__) == expected

    def test_in_memory_is_subclass(self) -> None:
        assert issubclass(InMemoryDispatchStore, DispatchStore)

    def test_sqlite_is_subclass(self) -> None:
        assert issubclass(SqliteDispatchStore, DispatchStore)

    def test_is_not_protocol(self) -> None:
        """Rule 7: ABC, not Protocol."""
        from typing import Protocol

        assert not issubclass(DispatchStore, Protocol)

    def test_in_memory_no_abstract_methods(self) -> None:
        assert len(InMemoryDispatchStore.__abstractmethods__) == 0

    def test_sqlite_no_abstract_methods(self) -> None:
        assert len(SqliteDispatchStore.__abstractmethods__) == 0


# ── Parametrized record/query/clear tests ─────────────────────────────────


@pytest.mark.parametrize("kind", STORE_KINDS)
class TestDispatchStoreRecordQueryClear:
    def test_record_and_query_all(self, kind: str) -> None:
        store = _store_factory(kind)()
        e1 = _make_event("a#0", "b", {"x": 1})
        e2 = _make_event("b#1", GraphNode.END)
        store.record(e1, "run-1")
        store.record(e2, "run-1")
        result = store.query_all("run-1")
        assert len(result) == 2
        assert result[0] == e1
        assert result[1] == e2

    def test_query_by_target(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.record(_make_event("a#0", "b"), "run-1")
        store.record(_make_event("a#0", "c"), "run-1")
        store.record(_make_event("b#1", "b"), "run-1")
        result = store.query_by_target("b", "run-1")
        assert len(result) == 2
        assert all(e.target == "b" for e in result)

    def test_query_by_source(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.record(_make_event("a#0", "b"), "run-1")
        store.record(_make_event("a#0", "c"), "run-1")
        store.record(_make_event("b#1", "b"), "run-1")
        result = store.query_by_source("a#0", "run-1")
        assert len(result) == 2
        assert all(e.source_instance == "a#0" for e in result)

    def test_clear(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.record(_make_event(), "run-1")
        store.record(_make_event(), "run-1")
        store.clear("run-1")
        assert store.query_all("run-1") == []

    def test_clear_only_affects_specified_run(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.record(_make_event(), "run-1")
        store.record(_make_event(), "run-2")
        store.clear("run-1")
        assert store.query_all("run-1") == []
        assert len(store.query_all("run-2")) == 1

    def test_different_run_ids_isolated(self, kind: str) -> None:
        store = _store_factory(kind)()
        store.record(_make_event("a#0", "b"), "run-1")
        store.record(_make_event("a#0", "b"), "run-2")
        assert len(store.query_all("run-1")) == 1
        assert len(store.query_all("run-2")) == 1
        assert store.query_all("run-1")[0].source_instance == "a#0"

    def test_payload_none_round_trip(self, kind: str) -> None:
        store = _store_factory(kind)()
        e = _make_event("a#0", "b", payload=None)
        store.record(e, "run-1")
        result = store.query_all("run-1")
        assert len(result) == 1
        assert result[0].payload is None

    def test_payload_dict_round_trip(self, kind: str) -> None:
        store = _store_factory(kind)()
        payload = {"key": "value", "num": 42, "nested": {"inner": [1, 2, 3]}}
        e = _make_event("a#0", "b", payload=payload)
        store.record(e, "run-1")
        result = store.query_all("run-1")
        assert len(result) == 1
        assert result[0].payload == payload

    def test_query_empty_run_returns_empty(self, kind: str) -> None:
        store = _store_factory(kind)()
        assert store.query_all("nonexistent") == []
        assert store.query_by_target("x", "nonexistent") == []
        assert store.query_by_source("y", "nonexistent") == []

    def test_preserves_insertion_order(self, kind: str) -> None:
        store = _store_factory(kind)()
        events = [
            _make_event("a#0", "b"),
            _make_event("a#0", "c"),
            _make_event("b#1", GraphNode.END),
        ]
        for e in events:
            store.record(e, "run-1")
        result = store.query_all("run-1")
        assert result == events

    def test_clear_nonexistent_run_is_noop(self, kind: str) -> None:
        store = _store_factory(kind)()
        # Should not raise.
        store.clear("nonexistent")


# ── SqliteDispatchStore specifics ─────────────────────────────────────────


class TestSqliteDispatchStoreSpecifics:
    def test_create_table_idempotent(self) -> None:
        """Constructing twice on the same file should not error (IF NOT EXISTS)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "dispatch.db")
            store1 = SqliteDispatchStore(db_path)
            store1.record(_make_event(), "run-1")
            store1.close()
            # Second construction on the same file — schema already exists.
            store2 = SqliteDispatchStore(db_path)
            assert len(store2.query_all("run-1")) == 1
            store2.close()

    def test_timestamps_are_epoch_ms(self) -> None:
        """created_at_ms column stores INTEGER epoch milliseconds (ADR-0029)."""
        from modex_graph.dispatch_store import _COL_CREATED_AT_MS, _DISPATCH_TABLE

        store = SqliteDispatchStore(":memory:")
        store.record(_make_event(), "run-1")
        row = store._conn.execute(f"SELECT {_COL_CREATED_AT_MS} FROM {_DISPATCH_TABLE}").fetchone()
        assert row is not None
        ts = row[0]
        assert isinstance(ts, int)
        # Sanity: epoch ms is ~1.7e12 in 2024-2026.
        assert ts > 1_700_000_000_000
        store.close()

    def test_table_and_column_constants(self) -> None:
        """Table/column names come from module constants (rule 14)."""
        from modex_graph.dispatch_store import (
            _COL_CREATED_AT_MS,
            _COL_ID,
            _COL_PAYLOAD,
            _COL_RUN_ID,
            _COL_SOURCE_INSTANCE,
            _COL_TARGET,
            _DISPATCH_TABLE,
        )

        assert _DISPATCH_TABLE == "dispatch_events"
        assert _COL_ID == "id"
        assert _COL_RUN_ID == "run_id"
        assert _COL_SOURCE_INSTANCE == "source_instance"
        assert _COL_TARGET == "target"
        assert _COL_PAYLOAD == "payload"
        assert _COL_CREATED_AT_MS == "created_at_ms"

    def test_file_based_persistence(self) -> None:
        """Data persists across store instances on the same file path."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "dispatch.db")
            store1 = SqliteDispatchStore(db_path)
            store1.record(_make_event("a#0", "b", {"k": "v"}), "run-1")
            store1.close()
            store2 = SqliteDispatchStore(db_path)
            result = store2.query_all("run-1")
            assert len(result) == 1
            assert result[0].payload == {"k": "v"}
            store2.close()

    def test_indexes_created(self) -> None:
        """Indexes exist for run_id, (run_id, target), (run_id, source_instance)."""
        store = SqliteDispatchStore(":memory:")
        indexes = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
            ("dispatch_events",),
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "idx_dispatch_events_run_id" in index_names
        assert "idx_dispatch_events_run_target" in index_names
        assert "idx_dispatch_events_run_source" in index_names
        store.close()

    def test_payload_null_stored_as_sql_null(self) -> None:
        """None payload is stored as SQL NULL, not the string 'null'."""
        from modex_graph.dispatch_store import _COL_PAYLOAD, _DISPATCH_TABLE

        store = SqliteDispatchStore(":memory:")
        store.record(_make_event("a#0", "b", payload=None), "run-1")
        row = store._conn.execute(f"SELECT {_COL_PAYLOAD} FROM {_DISPATCH_TABLE}").fetchone()
        assert row is not None
        assert row[0] is None  # SQL NULL, not "null"
        store.close()


# ── now_ms helper ─────────────────────────────────────────────────────────


class TestNowMs:
    def test_returns_int(self) -> None:
        from modex_graph.dispatch_store import now_ms

        ts = now_ms()
        assert isinstance(ts, int)

    def test_epoch_milliseconds(self) -> None:
        from modex_graph.dispatch_store import now_ms

        ts = now_ms()
        assert ts > 1_700_000_000_000

    def test_monotonicish(self) -> None:
        """Two calls return non-decreasing values."""
        from modex_graph.dispatch_store import now_ms

        t1 = now_ms()
        t2 = now_ms()
        assert t2 >= t1


# ── ParallelScheduler integration ────────────────────────────────────────


class _DispatchAddNode(Node[CounterState]):
    """Increments count, then dispatches to `target` if set."""

    def __init__(self, amount: int, target: str | None = None) -> None:
        self.amount = amount
        self.target = target

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += self.amount
        if self.target is not None:
            self.deliver(None, self.target, ctx)
        return NodeResult()


class _DispatchAddWithPayloadNode(Node[CounterState]):
    """Increments count, dispatches to `target` with a payload dict."""

    def __init__(self, amount: int, target: str, payload: dict[str, Any]) -> None:
        self.amount = amount
        self.target = target
        self.payload = payload

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += self.amount
        self.deliver(self.payload, self.target, ctx)
        return NodeResult()


def _make_parallel_ctx(
    state: CounterState | None = None,
) -> GraphContext[CounterState]:
    return GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
        coordinator=make_coordinator(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )


def _make_linear_graph(
    scheduler_kind: SchedulerKind = SchedulerKind.PARALLEL,
) -> tuple[Graph[CounterState], Any]:
    """Build a linear A→B→END graph. Returns (graph, compiled)."""
    from typing import cast

    g: Graph[CounterState] = Graph()
    g.add_node("a", _DispatchAddNode(amount=1, target="b"))
    g.add_node("b", _DispatchAddNode(amount=2, target=GraphNode.END))
    g.add_edge(GraphNode.START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", GraphNode.END)
    return g, cast(Any, g.compile(scheduler=scheduler_kind))


class TestParallelSchedulerWithDispatchStore:
    def test_defaults_to_in_memory_store(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        scheduler = ParallelScheduler(compiled)
        assert isinstance(scheduler._dispatch_store, InMemoryDispatchStore)

    def test_accepts_custom_store(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        store = InMemoryDispatchStore()
        scheduler = ParallelScheduler(compiled, dispatch_store=store)
        assert scheduler._dispatch_store is store

    async def test_records_dispatches_to_in_memory_store(self) -> None:
        _, compiled = _make_linear_graph()
        store = InMemoryDispatchStore()
        scheduler = ParallelScheduler(compiled, dispatch_store=store)
        ctx = _make_parallel_ctx(CounterState(count=0))
        await scheduler.run_async(ctx)

        assert scheduler._run_id is not None
        events = store.query_all(scheduler._run_id)
        assert len(events) == 2
        assert events[0].source_instance == "a#0"
        assert events[0].target == "b"
        assert events[1].source_instance == "b#1"
        assert events[1].target == GraphNode.END

    async def test_records_dispatches_to_sqlite_store(self) -> None:
        _, compiled = _make_linear_graph()
        store = SqliteDispatchStore(":memory:")
        scheduler = ParallelScheduler(compiled, dispatch_store=store)
        ctx = _make_parallel_ctx(CounterState(count=0))
        await scheduler.run_async(ctx)

        events = store.query_all(scheduler._run_id)  # type: ignore[arg-type]
        assert len(events) == 2
        assert events[0].source_instance == "a#0"
        assert events[1].target == GraphNode.END
        store.close()

    async def test_dispatch_log_property_reads_from_store(self) -> None:
        """The _dispatch_log property returns events from the store (backward compat)."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", _DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = _make_parallel_ctx(CounterState(count=0))
        engine = GraphEngine(compiled)
        await engine.run_async(ctx)

        scheduler = engine._scheduler
        assert isinstance(scheduler, ParallelScheduler)
        assert len(scheduler._dispatch_log) == 1
        assert scheduler._dispatch_log[0].target == GraphNode.END

    async def test_dispatch_log_empty_before_run(self) -> None:
        """_dispatch_log property returns [] before run_async sets run_id."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", _DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)
        scheduler = ParallelScheduler(compiled)
        assert scheduler._dispatch_log == []

    async def test_multiple_runs_isolated(self) -> None:
        """Two runs of the same engine produce different run_ids + isolated events."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", _DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        engine = GraphEngine(compiled)

        ctx1 = _make_parallel_ctx(CounterState(count=0))
        await engine.run_async(ctx1)
        run_id_1 = engine._scheduler._run_id

        ctx2 = _make_parallel_ctx(CounterState(count=0))
        await engine.run_async(ctx2)
        run_id_2 = engine._scheduler._run_id

        assert run_id_1 is not None
        assert run_id_2 is not None
        assert run_id_1 != run_id_2
        store = engine._scheduler._dispatch_store
        events_1 = store.query_all(run_id_1)
        events_2 = store.query_all(run_id_2)
        assert len(events_1) == 1
        assert len(events_2) == 1
        # Each run's event is from its own a#0 instance.
        assert events_1[0].source_instance == "a#0"
        assert events_2[0].source_instance == "a#0"

    async def test_query_by_source_via_scheduler(self) -> None:
        """query_by_source returns dispatches from a specific instance."""
        _, compiled = _make_linear_graph()
        store = InMemoryDispatchStore()
        scheduler = ParallelScheduler(compiled, dispatch_store=store)
        ctx = _make_parallel_ctx(CounterState(count=0))
        await scheduler.run_async(ctx)

        run_id = scheduler._run_id
        assert run_id is not None
        a_dispatches = store.query_by_source("a#0", run_id)
        b_dispatches = store.query_by_source("b#1", run_id)
        assert len(a_dispatches) == 1
        assert a_dispatches[0].target == "b"
        assert len(b_dispatches) == 1
        assert b_dispatches[0].target == GraphNode.END

    async def test_query_by_target_via_scheduler(self) -> None:
        """query_by_target returns dispatches to a specific target node."""
        _, compiled = _make_linear_graph()
        store = InMemoryDispatchStore()
        scheduler = ParallelScheduler(compiled, dispatch_store=store)
        ctx = _make_parallel_ctx(CounterState(count=0))
        await scheduler.run_async(ctx)

        run_id = scheduler._run_id
        assert run_id is not None
        to_b = store.query_by_target("b", run_id)
        to_end = store.query_by_target(GraphNode.END, run_id)
        assert len(to_b) == 1
        assert to_b[0].source_instance == "a#0"
        assert len(to_end) == 1
        assert to_end[0].source_instance == "b#1"

    async def test_payload_recorded_in_store(self) -> None:
        """Dispatch payload is preserved in the store."""
        g: Graph[CounterState] = Graph()
        g.add_node(
            "a",
            _DispatchAddWithPayloadNode(amount=1, target=GraphNode.END, payload={"data": 42}),
        )
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        store = InMemoryDispatchStore()
        scheduler = ParallelScheduler(compiled, dispatch_store=store)
        ctx = _make_parallel_ctx(CounterState(count=0))
        await scheduler.run_async(ctx)

        events = store.query_all(scheduler._run_id)  # type: ignore[arg-type]
        assert len(events) == 1
        assert events[0].payload and events[0].payload["delivered"] == {"data": 42}

    async def test_clear_removes_run_events(self) -> None:
        """clear(run_id) removes events for that run from the store."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", _DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        store = InMemoryDispatchStore()
        scheduler = ParallelScheduler(compiled, dispatch_store=store)
        ctx = _make_parallel_ctx(CounterState(count=0))
        await scheduler.run_async(ctx)

        run_id = scheduler._run_id
        assert run_id is not None
        assert len(store.query_all(run_id)) == 1
        store.clear(run_id)
        assert store.query_all(run_id) == []

    async def test_run_id_is_unique_string(self) -> None:
        """run_id is a non-empty string, unique per run."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", _DispatchAddNode(amount=1, target=GraphNode.END))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        scheduler = ParallelScheduler(compiled)
        assert scheduler._run_id is None  # before run
        await scheduler.run_async(_make_parallel_ctx(CounterState()))
        run_id_1 = scheduler._run_id
        assert isinstance(run_id_1, str)
        assert len(run_id_1) > 0

        await scheduler.run_async(_make_parallel_ctx(CounterState()))
        run_id_2 = scheduler._run_id
        assert run_id_2 != run_id_1
