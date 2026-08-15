# ruff: noqa: ANN401, S101

"""Tests for NodeStateStore ABC + Null / InMemory / Sqlite impls.

Covers:

- `NodeStateStore` ABC (rule 7: ABC, not Protocol): lifecycle + query methods.
- `NullNodeStateStore`: begin returns valid context, all else no-op.
- `InMemoryNodeStateStore`: lifecycle transitions, CAS semantics,
  version chain, orphan cleanup, finalize safety net, query methods.
- `SqliteNodeStateStore`: same lifecycle + CAS via UPDATE ... WHERE,
  schema creation, file-based persistence, timestamps, close.
- `InvocationStateError` raised on CAS failure (complete/cancel
  on already-terminal record).
- `InvocationStatus` enum has no PENDING / SUPERSEDED.
"""

from __future__ import annotations

import sqlite3
import tempfile
from abc import ABC
from pathlib import Path

import pytest

from modex_graph import (
    InMemoryNodeStateStore,
    InvocationStateError,
    InvocationStatus,
    NodeStateStore,
    NullNodeStateStore,
    SqliteNodeStateStore,
)

_GRAPH_INSTANCE_ID = 1001


def _store_factory(kind: str, gid: int = _GRAPH_INSTANCE_ID) -> NodeStateStore:
    if kind == "null":
        return NullNodeStateStore(gid)
    if kind == "memory":
        return InMemoryNodeStateStore(gid)
    if kind == "sqlite":
        return SqliteNodeStateStore(sqlite3.connect(":memory:"), gid)
    raise ValueError(f"unknown kind: {kind}")


LIFECYCLE_KINDS = ["null", "memory", "sqlite"]
PERSISTED_KINDS = ["memory", "sqlite"]


# ── InvocationStatus enum ─────────────────────────────────────────────


class TestInvocationStatusEnum:
    def test_no_pending(self) -> None:
        assert not hasattr(InvocationStatus, "PENDING")

    def test_no_superseded(self) -> None:
        assert not hasattr(InvocationStatus, "SUPERSEDED")

    def test_values(self) -> None:
        assert {s.value for s in InvocationStatus} == {
            "running",
            "completed",
            "canceled",
            "crashed",
        }


# ── NodeStateStore ABC ────────────────────────────────────────────────


class TestNodeStateStoreABC:
    def test_is_abc(self) -> None:
        assert issubclass(NodeStateStore, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            NodeStateStore(0)  # type: ignore[abstract]

    def test_null_is_subclass(self) -> None:
        assert issubclass(NullNodeStateStore, NodeStateStore)

    def test_in_memory_is_subclass(self) -> None:
        assert issubclass(InMemoryNodeStateStore, NodeStateStore)

    def test_sqlite_is_subclass(self) -> None:
        assert issubclass(SqliteNodeStateStore, NodeStateStore)

    def test_null_no_abstract_methods(self) -> None:
        assert len(NullNodeStateStore.__abstractmethods__) == 0

    def test_in_memory_no_abstract_methods(self) -> None:
        assert len(InMemoryNodeStateStore.__abstractmethods__) == 0

    def test_sqlite_no_abstract_methods(self) -> None:
        assert len(SqliteNodeStateStore.__abstractmethods__) == 0


# ── NullNodeStateStore ────────────────────────────────────────────────


class TestNullNodeStateStore:
    def test_begin_returns_valid_context(self) -> None:
        store = NullNodeStateStore(0)
        inv = store.begin_invocation("node_a")
        assert inv.invocation_id > 0
        assert inv.node_id == "node_a"
        assert inv.version == 0
        assert inv.parent_version is None

    def test_lifecycle_methods_are_noop(self) -> None:
        store = NullNodeStateStore(0)
        inv = store.begin_invocation("node_a")
        store.complete_invocation(inv)
        store.crash_invocation(inv)
        store.cancel_invocation(inv)
        store.finalize_invocation(inv)

    def test_queries_return_none_or_empty(self) -> None:
        store = NullNodeStateStore(0)
        assert store.load_latest("node_a") is None
        assert store.load_latest_completed("node_a") is None
        assert store.query_versions("node_a") == []
        assert store.list_nodes() == []
        assert store.query_all({InvocationStatus.RUNNING}) == []

    def test_graph_instance_id_captured(self) -> None:
        store = NullNodeStateStore(42)
        assert store.graph_instance_id == 42


# ── Parametrized lifecycle tests (memory + sqlite) ────────────────────


@pytest.mark.parametrize("kind", PERSISTED_KINDS)
class TestLifecycleTransitions:
    def test_begin_creates_running_record(self, kind: str) -> None:
        store = _store_factory(kind)
        inv = store.begin_invocation("worker")
        assert inv.invocation_id > 0
        assert inv.version == 0
        assert inv.parent_version is None

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.RUNNING
        assert latest.invocation_id == inv.invocation_id

    def test_complete_transitions_to_completed(self, kind: str) -> None:
        store = _store_factory(kind)
        inv = store.begin_invocation("worker")
        store.complete_invocation(inv)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

    def test_cancel_transitions_to_canceled(self, kind: str) -> None:
        store = _store_factory(kind)
        inv = store.begin_invocation("worker")
        store.cancel_invocation(inv)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.CANCELED

    def test_crash_transitions_to_crashed(self, kind: str) -> None:
        store = _store_factory(kind)
        inv = store.begin_invocation("worker")
        store.crash_invocation(inv)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED

    def test_crash_is_tolerant_on_terminal(self, kind: str) -> None:
        store = _store_factory(kind)
        inv = store.begin_invocation("worker")
        store.complete_invocation(inv)
        store.crash_invocation(inv)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

    def test_finalize_orphan_running_to_crashed(self, kind: str) -> None:
        store = _store_factory(kind)
        inv = store.begin_invocation("worker")
        store.finalize_invocation(inv)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED

    def test_finalize_skips_terminal(self, kind: str) -> None:
        store = _store_factory(kind)
        inv = store.begin_invocation("worker")
        store.complete_invocation(inv)
        store.finalize_invocation(inv)

        latest = store.load_latest("worker")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED


# ── CAS strictness (complete / cancel raise on lost race) ──


@pytest.mark.parametrize("kind", PERSISTED_KINDS)
class TestCASStrictness:
    def test_complete_on_completed_raises(self, kind: str) -> None:
        store = _store_factory(kind)
        inv = store.begin_invocation("worker")
        store.complete_invocation(inv)
        with pytest.raises(InvocationStateError, match="CAS failed"):
            store.complete_invocation(inv)

    def test_cancel_on_completed_raises(self, kind: str) -> None:
        store = _store_factory(kind)
        inv = store.begin_invocation("worker")
        store.complete_invocation(inv)
        with pytest.raises(InvocationStateError, match="CAS failed"):
            store.cancel_invocation(inv)

    def test_complete_on_canceled_raises(self, kind: str) -> None:
        store = _store_factory(kind)
        inv = store.begin_invocation("worker")
        store.cancel_invocation(inv)
        with pytest.raises(InvocationStateError, match="CAS failed"):
            store.complete_invocation(inv)


# ── Version chain + orphan cleanup ────────────────────────────────────


@pytest.mark.parametrize("kind", PERSISTED_KINDS)
class TestVersionChain:
    def test_version_increments(self, kind: str) -> None:
        store = _store_factory(kind)
        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0)

        inv1 = store.begin_invocation("worker")
        assert inv1.version == 1
        assert inv1.parent_version == 0
        store.complete_invocation(inv1)

        inv2 = store.begin_invocation("worker")
        assert inv2.version == 2
        assert inv2.parent_version == 1

    def test_parent_version_from_latest_completed(self, kind: str) -> None:
        store = _store_factory(kind)
        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0)

        inv1 = store.begin_invocation("worker")
        store.crash_invocation(inv1)

        inv2 = store.begin_invocation("worker")
        assert inv2.parent_version == 0
        assert inv2.parent_version != 1

    def test_orphan_running_marked_crashed_on_begin(self, kind: str) -> None:
        store = _store_factory(kind)
        inv0 = store.begin_invocation("worker")
        store.begin_invocation("worker")

        versions = store.query_versions("worker", {InvocationStatus.CRASHED})
        assert len(versions) == 1
        assert versions[0].invocation_id == inv0.invocation_id


# ── Query methods ─────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", PERSISTED_KINDS)
class TestQueryMethods:
    def test_load_latest_completed(self, kind: str) -> None:
        store = _store_factory(kind)
        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0)

        inv1 = store.begin_invocation("worker")
        store.crash_invocation(inv1)

        completed = store.load_latest_completed("worker")
        assert completed is not None
        assert completed.invocation_id == inv0.invocation_id

    def test_load_latest_returns_none_for_missing(self, kind: str) -> None:
        store = _store_factory(kind)
        assert store.load_latest("nonexistent") is None

    def test_load_by_invocation_id(self, kind: str) -> None:
        store = _store_factory(kind)
        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0)
        inv1 = store.begin_invocation("worker")
        store.crash_invocation(inv1)

        found = store.load_by_invocation_id("worker", inv1.invocation_id)
        assert found is not None
        assert found.invocation_id == inv1.invocation_id
        assert found.status == InvocationStatus.CRASHED

    def test_load_by_invocation_id_returns_none_for_missing(self, kind: str) -> None:
        store = _store_factory(kind)
        store.begin_invocation("worker")
        assert store.load_by_invocation_id("worker", 999999) is None
        assert store.load_by_invocation_id("nonexistent", 1) is None

    def test_query_versions_with_filter(self, kind: str) -> None:
        store = _store_factory(kind)
        inv0 = store.begin_invocation("worker")
        store.complete_invocation(inv0)
        inv1 = store.begin_invocation("worker")
        store.crash_invocation(inv1)

        crashed = store.query_versions("worker", {InvocationStatus.CRASHED})
        assert len(crashed) == 1
        assert crashed[0].invocation_id == inv1.invocation_id

    def test_query_versions_ordered_desc(self, kind: str) -> None:
        store = _store_factory(kind)
        for _i in range(3):
            inv = store.begin_invocation("worker")
            store.complete_invocation(inv)

        versions = store.query_versions("worker")
        assert [v.version for v in versions] == [2, 1, 0]

    def test_list_nodes(self, kind: str) -> None:
        store = _store_factory(kind)
        store.begin_invocation("node_a")
        store.begin_invocation("node_b")
        nodes = store.list_nodes()
        assert set(nodes) == {"node_a", "node_b"}

    def test_query_all(self, kind: str) -> None:
        store = _store_factory(kind)
        inv_a = store.begin_invocation("node_a")
        store.complete_invocation(inv_a)
        inv_b = store.begin_invocation("node_b")
        store.crash_invocation(inv_b)

        all_completed = store.query_all({InvocationStatus.COMPLETED})
        assert len(all_completed) == 1
        assert all_completed[0].node_id == "node_a"

    def test_clear(self, kind: str) -> None:
        store = _store_factory(kind)
        store.begin_invocation("node_a")
        store.begin_invocation("node_b")
        store.clear()
        assert store.list_nodes() == []
        assert store.load_latest("node_a") is None


# ── SqliteNodeStateStore specifics ────────────────────────────────────


class TestSqliteNodeStateStoreSpecifics:
    def test_file_based_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "node_states.db")
            conn1 = sqlite3.connect(db_path)
            store1 = SqliteNodeStateStore(conn1, _GRAPH_INSTANCE_ID)
            inv = store1.begin_invocation("worker")
            store1.complete_invocation(inv)
            conn1.close()

            conn2 = sqlite3.connect(db_path)
            store2 = SqliteNodeStateStore(conn2, _GRAPH_INSTANCE_ID)
            latest = store2.load_latest("worker")
            assert latest is not None
            assert latest.status == InvocationStatus.COMPLETED
            conn2.close()

    def test_check_constraint_no_pending_or_superseded(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteNodeStateStore(conn, _GRAPH_INSTANCE_ID)
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO node_states (node_state_id, graph_instance_id, "
                "node_id, version, status, invocation_id, created_at, updated_at) "
                "VALUES (1, ?, 'n', 0, 'pending', 0, 0, 0)",
                (_GRAPH_INSTANCE_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO node_states (node_state_id, graph_instance_id, "
                "node_id, version, status, invocation_id, created_at, updated_at) "
                "VALUES (2, ?, 'n', 0, 'superseded', 0, 0, 0)",
                (_GRAPH_INSTANCE_ID,),
            )
        conn.close()

    def test_schema_has_all_columns(self) -> None:
        conn = sqlite3.connect(":memory:")
        store = SqliteNodeStateStore(conn, _GRAPH_INSTANCE_ID)
        columns = {
            row[1]
            for row in store._conn.execute("PRAGMA table_info(node_states)").fetchall()
        }
        assert {
            "node_state_id",
            "graph_instance_id",
            "node_id",
            "version",
            "parent_version",
            "status",
            "invocation_id",
            "created_at",
            "updated_at",
        } <= columns
        conn.close()
