"""Tests for CheckpointStore ABC + Memory/Sqlite implementations (ADR-0034 D19).

Covers:

- `CheckpointStore` ABC is abstract (cannot instantiate directly).
- `MemoryCheckpointStore` — dict-backed default: save -> load_latest returns
  latest, multiple saves -> load_latest returns last, clear -> load_latest
  returns None.
- `SqliteCheckpointStore` with `:memory:` — same behavior as Memory.
- `CheckpointData` round-trip: create with all fields, save, load, verify
  all fields preserved (main_state, pending_on_all_preds,
  completed_instances, dispatch_events).
- `InstanceRecord` frozen model (extra="forbid", cannot set fields).
- `CheckpointData` is frozen (cannot set fields after construction).
- Cross-run isolation: checkpoints under different run_ids are isolated.
- clear on non-existent run_id is a no-op.
- load_latest on non-existent run_id returns None.
- Ticket 10 class 1 new fields (graph_instance_id, activated_sources,
  instance_seq, iteration_count): defaults, backward compatibility,
  round-trip preservation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_graph import (
    CheckpointData,
    CheckpointStore,
    DispatchEvent,
    InstanceRecord,
    MemoryCheckpointStore,
    SqliteCheckpointStore,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def make_checkpoint(
    *,
    count: int = 0,
    name: str = "init",
    instance_id: str = "a#0",
    fork_version: int = 0,
    status: str = "completed",
) -> CheckpointData:
    """Build a CheckpointData with all fields populated."""
    return CheckpointData(
        main_state={"count": count, "name": name},
        pending_on_all_preds={
            "d": {"b": [{"x": 1}], "c": [None]},
        },
        completed_instances=[
            InstanceRecord(
                instance_id=instance_id,
                node_name=instance_id.split("#")[0],
                fork_version=fork_version,
                status=status,
            ),
        ],
        dispatch_events=[
            DispatchEvent(
                source_instance="a#0",
                target="__end__",
                payload={"key": "value"},
            ),
        ],
    )


# ── CheckpointStore ABC ────────────────────────────────────────────────────


class TestCheckpointStoreABC:
    def test_is_abstract(self) -> None:
        from abc import ABC

        assert issubclass(CheckpointStore, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            CheckpointStore()  # type: ignore[abstract]

    def test_three_abstract_methods(self) -> None:
        expected = {"save", "load_latest", "clear"}
        assert set(CheckpointStore.__abstractmethods__) == expected

    def test_all_methods_are_async(self) -> None:
        import inspect

        for method_name in ("save", "load_latest", "clear"):
            method = getattr(CheckpointStore, method_name)
            assert inspect.iscoroutinefunction(method), f"{method_name} should be async"


# ── MemoryCheckpointStore is concrete ─────────────────────────────────────


class TestMemoryCheckpointStoreIsConcrete:
    def test_inherits_abc(self) -> None:
        assert issubclass(MemoryCheckpointStore, CheckpointStore)

    def test_no_abstract_methods(self) -> None:
        assert len(MemoryCheckpointStore.__abstractmethods__) == 0

    def test_can_instantiate(self) -> None:
        store = MemoryCheckpointStore()
        assert isinstance(store, CheckpointStore)


# ── SqliteCheckpointStore is concrete ─────────────────────────────────────


class TestSqliteCheckpointStoreIsConcrete:
    def test_inherits_abc(self) -> None:
        assert issubclass(SqliteCheckpointStore, CheckpointStore)

    def test_no_abstract_methods(self) -> None:
        assert len(SqliteCheckpointStore.__abstractmethods__) == 0

    def test_can_instantiate_with_memory_db(self) -> None:
        store = SqliteCheckpointStore(":memory:")
        assert isinstance(store, CheckpointStore)
        store.close()


# ── MemoryCheckpointStore behavior ────────────────────────────────────────


class TestMemoryCheckpointStoreBehavior:
    async def test_save_then_load_latest_returns_it(self) -> None:
        store = MemoryCheckpointStore()
        data = make_checkpoint(count=42)
        await store.save(data, "run-1")
        loaded = await store.load_latest("run-1")
        assert loaded is not None
        assert loaded.main_state["count"] == 42

    async def test_multiple_saves_load_latest_returns_last(self) -> None:
        store = MemoryCheckpointStore()
        await store.save(make_checkpoint(count=1), "run-1")
        await store.save(make_checkpoint(count=2), "run-1")
        await store.save(make_checkpoint(count=3), "run-1")
        loaded = await store.load_latest("run-1")
        assert loaded is not None
        assert loaded.main_state["count"] == 3

    async def test_clear_then_load_latest_returns_none(self) -> None:
        store = MemoryCheckpointStore()
        await store.save(make_checkpoint(count=1), "run-1")
        await store.clear("run-1")
        loaded = await store.load_latest("run-1")
        assert loaded is None

    async def test_load_latest_on_nonexistent_run_returns_none(self) -> None:
        store = MemoryCheckpointStore()
        loaded = await store.load_latest("nonexistent")
        assert loaded is None

    async def test_clear_on_nonexistent_run_is_noop(self) -> None:
        store = MemoryCheckpointStore()
        await store.clear("nonexistent")

    async def test_different_run_ids_isolated(self) -> None:
        store = MemoryCheckpointStore()
        await store.save(make_checkpoint(count=10), "run-a")
        await store.save(make_checkpoint(count=20), "run-b")
        loaded_a = await store.load_latest("run-a")
        loaded_b = await store.load_latest("run-b")
        assert loaded_a is not None
        assert loaded_b is not None
        assert loaded_a.main_state["count"] == 10
        assert loaded_b.main_state["count"] == 20

    async def test_clear_only_affects_specified_run(self) -> None:
        store = MemoryCheckpointStore()
        await store.save(make_checkpoint(count=1), "run-a")
        await store.save(make_checkpoint(count=2), "run-b")
        await store.clear("run-a")
        assert await store.load_latest("run-a") is None
        loaded_b = await store.load_latest("run-b")
        assert loaded_b is not None
        assert loaded_b.main_state["count"] == 2


# ── SqliteCheckpointStore behavior (with :memory:) ────────────────────────


class TestSqliteCheckpointStoreBehavior:
    async def test_save_then_load_latest_returns_it(self) -> None:
        store = SqliteCheckpointStore(":memory:")
        try:
            data = make_checkpoint(count=42)
            await store.save(data, "run-1")
            loaded = await store.load_latest("run-1")
            assert loaded is not None
            assert loaded.main_state["count"] == 42
        finally:
            store.close()

    async def test_multiple_saves_load_latest_returns_last(self) -> None:
        store = SqliteCheckpointStore(":memory:")
        try:
            await store.save(make_checkpoint(count=1), "run-1")
            await store.save(make_checkpoint(count=2), "run-1")
            await store.save(make_checkpoint(count=3), "run-1")
            loaded = await store.load_latest("run-1")
            assert loaded is not None
            assert loaded.main_state["count"] == 3
        finally:
            store.close()

    async def test_clear_then_load_latest_returns_none(self) -> None:
        store = SqliteCheckpointStore(":memory:")
        try:
            await store.save(make_checkpoint(count=1), "run-1")
            await store.clear("run-1")
            loaded = await store.load_latest("run-1")
            assert loaded is None
        finally:
            store.close()

    async def test_load_latest_on_nonexistent_run_returns_none(self) -> None:
        store = SqliteCheckpointStore(":memory:")
        try:
            loaded = await store.load_latest("nonexistent")
            assert loaded is None
        finally:
            store.close()

    async def test_clear_on_nonexistent_run_is_noop(self) -> None:
        store = SqliteCheckpointStore(":memory:")
        try:
            await store.clear("nonexistent")
        finally:
            store.close()

    async def test_different_run_ids_isolated(self) -> None:
        store = SqliteCheckpointStore(":memory:")
        try:
            await store.save(make_checkpoint(count=10), "run-a")
            await store.save(make_checkpoint(count=20), "run-b")
            loaded_a = await store.load_latest("run-a")
            loaded_b = await store.load_latest("run-b")
            assert loaded_a is not None
            assert loaded_b is not None
            assert loaded_a.main_state["count"] == 10
            assert loaded_b.main_state["count"] == 20
        finally:
            store.close()

    async def test_clear_only_affects_specified_run(self) -> None:
        store = SqliteCheckpointStore(":memory:")
        try:
            await store.save(make_checkpoint(count=1), "run-a")
            await store.save(make_checkpoint(count=2), "run-b")
            await store.clear("run-a")
            assert await store.load_latest("run-a") is None
            loaded_b = await store.load_latest("run-b")
            assert loaded_b is not None
            assert loaded_b.main_state["count"] == 2
        finally:
            store.close()


# ── CheckpointData round-trip ─────────────────────────────────────────────


class TestCheckpointDataRoundTrip:
    async def test_round_trip_preserves_all_fields(self) -> None:
        store = MemoryCheckpointStore()
        original = make_checkpoint(
            count=99,
            name="after_merge",
            instance_id="llm#3",
            fork_version=2,
            status="completed",
        )
        await store.save(original, "run-rt")
        loaded = await store.load_latest("run-rt")
        assert loaded is not None

        assert loaded.main_state == {"count": 99, "name": "after_merge"}
        assert loaded.pending_on_all_preds == {
            "d": {"b": [{"x": 1}], "c": [None]},
        }
        assert len(loaded.completed_instances) == 1
        inst = loaded.completed_instances[0]
        assert inst.instance_id == "llm#3"
        assert inst.node_name == "llm"
        assert inst.fork_version == 2
        assert inst.status == "completed"
        assert len(loaded.dispatch_events) == 1
        evt = loaded.dispatch_events[0]
        assert evt.source_instance == "a#0"
        assert evt.target == "__end__"
        assert evt.payload == {"key": "value"}

    async def test_round_trip_sqlite_preserves_all_fields(self) -> None:
        store = SqliteCheckpointStore(":memory:")
        try:
            original = make_checkpoint(
                count=55,
                name="sqlite_round_trip",
                instance_id="tool#1",
                fork_version=1,
                status="completed",
            )
            await store.save(original, "run-sqlite-rt")
            loaded = await store.load_latest("run-sqlite-rt")
            assert loaded is not None

            assert loaded.main_state == {"count": 55, "name": "sqlite_round_trip"}
            assert loaded.pending_on_all_preds == {
                "d": {"b": [{"x": 1}], "c": [None]},
            }
            assert len(loaded.completed_instances) == 1
            inst = loaded.completed_instances[0]
            assert inst.instance_id == "tool#1"
            assert inst.node_name == "tool"
            assert inst.fork_version == 1
            assert inst.status == "completed"
            assert len(loaded.dispatch_events) == 1
            evt = loaded.dispatch_events[0]
            assert evt.source_instance == "a#0"
            assert evt.target == "__end__"
            assert evt.payload == {"key": "value"}
        finally:
            store.close()

    async def test_round_trip_with_empty_optionals(self) -> None:
        """CheckpointData with default empty completed_instances and
        dispatch_events round-trips correctly."""
        store = MemoryCheckpointStore()
        original = CheckpointData(
            main_state={"count": 0},
            pending_on_all_preds={},
        )
        await store.save(original, "run-empty")
        loaded = await store.load_latest("run-empty")
        assert loaded is not None
        assert loaded.main_state == {"count": 0}
        assert loaded.pending_on_all_preds == {}
        assert loaded.completed_instances == []
        assert loaded.dispatch_events == []

    async def test_round_trip_with_none_payload_in_dispatch(self) -> None:
        """DispatchEvent with payload=None round-trips through checkpoint."""
        store = MemoryCheckpointStore()
        original = CheckpointData(
            main_state={"count": 1},
            pending_on_all_preds={},
            dispatch_events=[
                DispatchEvent(source_instance="a#0", target="b"),
            ],
        )
        await store.save(original, "run-none-payload")
        loaded = await store.load_latest("run-none-payload")
        assert loaded is not None
        assert len(loaded.dispatch_events) == 1
        assert loaded.dispatch_events[0].payload is None


# ── InstanceRecord frozen model ───────────────────────────────────────────


class TestInstanceRecord:
    def test_is_pydantic_model(self) -> None:
        from pydantic import BaseModel

        assert issubclass(InstanceRecord, BaseModel)

    def test_frozen(self) -> None:
        assert InstanceRecord.model_config.get("frozen") is True

    def test_extra_forbid(self) -> None:
        assert InstanceRecord.model_config.get("extra") == "forbid"

    def test_fields(self) -> None:
        record = InstanceRecord(
            instance_id="llm#0",
            node_name="llm",
            fork_version=3,
            status="completed",
        )
        assert record.instance_id == "llm#0"
        assert record.node_name == "llm"
        assert record.fork_version == 3
        assert record.status == "completed"

    def test_frozen_immutable(self) -> None:
        record = InstanceRecord(
            instance_id="a#0",
            node_name="a",
            fork_version=0,
            status="completed",
        )
        with pytest.raises(ValidationError):
            record.instance_id = "b#1"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InstanceRecord(  # type: ignore[call-arg]
                instance_id="a#0",
                node_name="a",
                fork_version=0,
                status="completed",
                extra_field="bad",
            )

    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            InstanceRecord(  # type: ignore[call-arg]
                node_name="a",
                fork_version=0,
                status="completed",
            )


# ── CheckpointData frozen model ───────────────────────────────────────────


class TestCheckpointDataFrozen:
    def test_is_pydantic_model(self) -> None:
        from pydantic import BaseModel

        assert issubclass(CheckpointData, BaseModel)

    def test_frozen(self) -> None:
        assert CheckpointData.model_config.get("frozen") is True

    def test_extra_forbid(self) -> None:
        assert CheckpointData.model_config.get("extra") == "forbid"

    def test_frozen_immutable(self) -> None:
        data = CheckpointData(
            main_state={"count": 1},
            pending_on_all_preds={},
        )
        with pytest.raises(ValidationError):
            data.main_state = {"count": 2}  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CheckpointData(  # type: ignore[call-arg]
                main_state={"count": 1},
                pending_on_all_preds={},
                extra_field="bad",
            )

    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            CheckpointData(  # type: ignore[call-arg]
                pending_on_all_preds={},
            )

    def test_completed_instances_defaults_empty(self) -> None:
        data = CheckpointData(
            main_state={"count": 0},
            pending_on_all_preds={},
        )
        assert data.completed_instances == []
        assert data.dispatch_events == []


# ── Ticket 10 class 1 — new fields ────────────────────────────────────────


class TestCheckpointDataTicket10Fields:
    """`CheckpointData` new fields from ticket 10 class 1.

    - `graph_instance_id: int | None` — defaults to None.
    - `activated_sources: dict[str, list[str]]` — defaults to {}.
    - `instance_seq: int` — defaults to 0.
    - `iteration_count: int` — defaults to 0.
    """

    def test_new_fields_have_defaults(self) -> None:
        data = CheckpointData(
            main_state={"count": 0},
            pending_on_all_preds={},
        )
        assert data.graph_instance_id is None
        assert data.activated_sources == {}
        assert data.instance_seq == 0
        assert data.iteration_count == 0

    def test_graph_instance_id_is_int_or_none(self) -> None:
        data = CheckpointData(
            main_state={},
            pending_on_all_preds={},
            graph_instance_id=123456789,
        )
        assert data.graph_instance_id == 123456789
        assert isinstance(data.graph_instance_id, int)

    def test_graph_instance_id_none_explicit(self) -> None:
        data = CheckpointData(
            main_state={},
            pending_on_all_preds={},
            graph_instance_id=None,
        )
        assert data.graph_instance_id is None

    def test_activated_sources_populated(self) -> None:
        data = CheckpointData(
            main_state={},
            pending_on_all_preds={},
            activated_sources={"llm": ["tool", "retriever"]},
        )
        assert data.activated_sources == {"llm": ["tool", "retriever"]}

    def test_instance_seq_set(self) -> None:
        data = CheckpointData(
            main_state={},
            pending_on_all_preds={},
            instance_seq=7,
        )
        assert data.instance_seq == 7

    def test_iteration_count_set(self) -> None:
        data = CheckpointData(
            main_state={},
            pending_on_all_preds={},
            iteration_count=12,
        )
        assert data.iteration_count == 12

    def test_all_new_fields_set_together(self) -> None:
        data = CheckpointData(
            main_state={"v": 1},
            pending_on_all_preds={},
            graph_instance_id=999,
            activated_sources={"a": ["b", "c"]},
            instance_seq=3,
            iteration_count=5,
        )
        assert data.graph_instance_id == 999
        assert data.activated_sources == {"a": ["b", "c"]}
        assert data.instance_seq == 3
        assert data.iteration_count == 5

    def test_new_fields_frozen_immutable(self) -> None:
        data = CheckpointData(
            main_state={},
            pending_on_all_preds={},
            graph_instance_id=1,
        )
        with pytest.raises(ValidationError):
            data.graph_instance_id = 2  # type: ignore[misc]
        with pytest.raises(ValidationError):
            data.activated_sources = {"x": ["y"]}  # type: ignore[misc]
        with pytest.raises(ValidationError):
            data.instance_seq = 99  # type: ignore[misc]
        with pytest.raises(ValidationError):
            data.iteration_count = 99  # type: ignore[misc]

    def test_new_fields_extra_still_forbid(self) -> None:
        """extra='forbid' still applies — unknown fields rejected."""
        with pytest.raises(ValidationError):
            CheckpointData(  # type: ignore[call-arg]
                main_state={},
                pending_on_all_preds={},
                not_a_field="bad",
            )


class TestCheckpointDataTicket10BackwardCompat:
    """Existing construction (without the new fields) must still work.

    The new fields all have defaults, so any code that constructs
    `CheckpointData` with only the original four fields continues to work
    unchanged. This is the backward-compatibility contract.
    """

    def test_minimal_construction_still_works(self) -> None:
        data = CheckpointData(
            main_state={"x": 1},
            pending_on_all_preds={},
        )
        assert data.main_state == {"x": 1}
        assert data.pending_on_all_preds == {}
        assert data.completed_instances == []
        assert data.dispatch_events == []
        assert data.graph_instance_id is None
        assert data.activated_sources == {}
        assert data.instance_seq == 0
        assert data.iteration_count == 0

    def test_existing_make_checkpoint_helper_still_works(self) -> None:
        """The existing `make_checkpoint` helper (no new fields) still
        produces a valid CheckpointData with defaulted new fields."""
        data = make_checkpoint(count=5, name="compat")
        assert data.main_state == {"count": 5, "name": "compat"}
        assert data.graph_instance_id is None
        assert data.activated_sources == {}
        assert data.instance_seq == 0
        assert data.iteration_count == 0

    async def test_existing_round_trip_preserves_new_defaults(self) -> None:
        """Round-trip of a CheckpointData constructed without the new fields
        preserves the defaulted values."""
        store = MemoryCheckpointStore()
        original = CheckpointData(
            main_state={"count": 1},
            pending_on_all_preds={},
        )
        await store.save(original, "run-compat")
        loaded = await store.load_latest("run-compat")
        assert loaded is not None
        assert loaded.graph_instance_id is None
        assert loaded.activated_sources == {}
        assert loaded.instance_seq == 0
        assert loaded.iteration_count == 0


class TestCheckpointDataTicket10RoundTrip:
    """Round-trip persistence preserves the new fields."""

    async def test_round_trip_preserves_new_fields_memory(self) -> None:
        store = MemoryCheckpointStore()
        original = CheckpointData(
            main_state={"count": 1},
            pending_on_all_preds={},
            graph_instance_id=12345,
            activated_sources={"llm": ["tool", "retriever"], "tool": ["llm"]},
            instance_seq=42,
            iteration_count=7,
        )
        await store.save(original, "run-t10-mem")
        loaded = await store.load_latest("run-t10-mem")
        assert loaded is not None
        assert loaded.graph_instance_id == 12345
        assert loaded.activated_sources == {"llm": ["tool", "retriever"], "tool": ["llm"]}
        assert loaded.instance_seq == 42
        assert loaded.iteration_count == 7

    async def test_round_trip_preserves_new_fields_sqlite(self) -> None:
        store = SqliteCheckpointStore(":memory:")
        try:
            original = CheckpointData(
                main_state={"count": 1},
                pending_on_all_preds={},
                graph_instance_id=99_999_999,
                activated_sources={"a": ["b"]},
                instance_seq=100,
                iteration_count=25,
            )
            await store.save(original, "run-t10-sqlite")
            loaded = await store.load_latest("run-t10-sqlite")
            assert loaded is not None
            assert loaded.graph_instance_id == 99_999_999
            assert loaded.activated_sources == {"a": ["b"]}
            assert loaded.instance_seq == 100
            assert loaded.iteration_count == 25
        finally:
            store.close()

    async def test_round_trip_preserves_graph_instance_id_none(self) -> None:
        """graph_instance_id=None round-trips (not silently coerced to 0)."""
        store = MemoryCheckpointStore()
        original = CheckpointData(
            main_state={},
            pending_on_all_preds={},
            graph_instance_id=None,
        )
        await store.save(original, "run-none-gid")
        loaded = await store.load_latest("run-none-gid")
        assert loaded is not None
        assert loaded.graph_instance_id is None

    def test_model_dump_round_trip_preserves_new_fields(self) -> None:
        original = CheckpointData(
            main_state={"x": 1},
            pending_on_all_preds={},
            graph_instance_id=77,
            activated_sources={"n": ["s1", "s2"]},
            instance_seq=9,
            iteration_count=3,
        )
        restored = CheckpointData.model_validate(original.model_dump())
        assert restored == original
        assert restored.graph_instance_id == 77
        assert restored.activated_sources == {"n": ["s1", "s2"]}
        assert restored.instance_seq == 9
        assert restored.iteration_count == 3

    def test_model_dump_json_round_trip_preserves_new_fields(self) -> None:
        original = CheckpointData(
            main_state={},
            pending_on_all_preds={},
            graph_instance_id=55,
            activated_sources={"t": ["s"]},
            instance_seq=11,
            iteration_count=22,
        )
        json_str = original.model_dump_json()
        restored = CheckpointData.model_validate_json(json_str)
        assert restored == original
