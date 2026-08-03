"""Tests for `NodeState` ABC + `SimpleNodeState` impl.

Covers:

- `NodeState` ABC (rule 7: ABC, not Protocol): 10 abstract methods.
- `SimpleNodeState`:
    - CRUD: write -> read round-trip, overwrite.
    - `snapshot()` returns a shallow copy (mutating the returned dict
      does not affect internal state).
    - `restore(data)` replaces ALL current state (prior fields discarded).
    - `has()` returns True for written/restored fields, False otherwise.
    - Empty state: `snapshot()` returns `{}`, `read()` raises KeyError,
      `has()` returns False.
    - `read()` raises `KeyError` with a helpful message listing available
      fields.
    - Initial state constructor: shallow-copied (mutating the input dict
      after construction does not affect internal state).
    - `restore()` shallow-copies its input (mutating the input dict after
      restore does not affect internal state).
- Follows the EXACT pattern of `test_node_state_store.py`.
"""

from __future__ import annotations

from abc import ABC
from typing import Any

import pytest

from modex_graph import NodeState, SimpleNodeState

# ── NodeState ABC ─────────────────────────────────────────────────────────


class TestNodeStateABC:
    def test_is_abc(self) -> None:
        assert issubclass(NodeState, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            NodeState()  # type: ignore[abstract]

    def test_ten_abstract_methods(self) -> None:
        expected = {
            "read",
            "write",
            "snapshot",
            "restore",
            "has",
            "save_invocation",
            "load_invocation",
            "load_latest",
            "load_latest_completed",
            "query_versions",
        }
        assert set(NodeState.__abstractmethods__) == expected

    def test_simple_node_state_subclass(self) -> None:
        assert issubclass(SimpleNodeState, NodeState)


# ── SimpleNodeState: CRUD ─────────────────────────────────────────────────


class TestSimpleNodeStateCRUD:
    def test_write_then_read_round_trip(self) -> None:
        state = SimpleNodeState()
        state.write("count", 42)
        assert state.read("count") == 42

    def test_write_overwrites_existing(self) -> None:
        state = SimpleNodeState()
        state.write("count", 1)
        state.write("count", 2)
        assert state.read("count") == 2

    def test_write_multiple_fields(self) -> None:
        state = SimpleNodeState()
        state.write("count", 3)
        state.write("name", "alice")
        state.write("flag", True)
        assert state.read("count") == 3
        assert state.read("name") == "alice"
        assert state.read("flag") is True

    def test_write_accepts_any_type(self) -> None:
        state = SimpleNodeState()
        state.write("none", None)
        state.write("list", [1, 2, 3])
        state.write("dict", {"a": 1})
        state.write("nested", {"items": [1, {"k": "v"}]})
        assert state.read("none") is None
        assert state.read("list") == [1, 2, 3]
        assert state.read("dict") == {"a": 1}
        assert state.read("nested") == {"items": [1, {"k": "v"}]}

    def test_read_missing_raises_key_error(self) -> None:
        state = SimpleNodeState()
        with pytest.raises(KeyError, match="missing_field"):
            state.read("missing_field")

    def test_read_key_error_message_lists_available_fields(self) -> None:
        state = SimpleNodeState()
        state.write("alpha", 1)
        state.write("beta", 2)
        with pytest.raises(KeyError) as exc_info:
            state.read("gamma")
        message = str(exc_info.value)
        assert "gamma" in message
        assert "alpha" in message
        assert "beta" in message


# ── SimpleNodeState: has() ────────────────────────────────────────────────


class TestSimpleNodeStateHas:
    def test_has_returns_false_for_empty_state(self) -> None:
        state = SimpleNodeState()
        assert state.has("anything") is False

    def test_has_returns_true_after_write(self) -> None:
        state = SimpleNodeState()
        state.write("count", 5)
        assert state.has("count") is True

    def test_has_returns_false_for_unwritten_field(self) -> None:
        state = SimpleNodeState()
        state.write("count", 5)
        assert state.has("count") is True
        assert state.has("other") is False

    def test_has_returns_true_for_none_value(self) -> None:
        state = SimpleNodeState()
        state.write("none_field", None)
        assert state.has("none_field") is True
        assert state.read("none_field") is None


# ── SimpleNodeState: snapshot() ───────────────────────────────────────────


class TestSimpleNodeStateSnapshot:
    def test_snapshot_empty_state_returns_empty_dict(self) -> None:
        state = SimpleNodeState()
        assert state.snapshot() == {}

    def test_snapshot_returns_all_fields(self) -> None:
        state = SimpleNodeState()
        state.write("a", 1)
        state.write("b", "two")
        snap = state.snapshot()
        assert snap == {"a": 1, "b": "two"}

    def test_snapshot_returns_shallow_copy(self) -> None:
        state = SimpleNodeState()
        state.write("count", 10)
        snap = state.snapshot()
        snap["count"] = 999
        snap["new"] = "injected"
        # Mutating the returned dict must not affect internal state.
        assert state.read("count") == 10
        assert not state.has("new")

    def test_snapshot_reflects_latest_writes(self) -> None:
        state = SimpleNodeState()
        state.write("count", 1)
        snap1 = state.snapshot()
        state.write("count", 2)
        snap2 = state.snapshot()
        assert snap1 == {"count": 1}
        assert snap2 == {"count": 2}


# ── SimpleNodeState: restore() ────────────────────────────────────────────


class TestSimpleNodeStateRestore:
    def test_restore_replaces_all_state(self) -> None:
        state = SimpleNodeState()
        state.write("old_a", 1)
        state.write("old_b", 2)
        state.restore({"new_a": 10})
        assert not state.has("old_a")
        assert not state.has("old_b")
        assert state.has("new_a")
        assert state.read("new_a") == 10

    def test_restore_from_empty_dict_clears_state(self) -> None:
        state = SimpleNodeState()
        state.write("count", 5)
        state.restore({})
        assert state.snapshot() == {}
        assert not state.has("count")

    def test_restore_then_read_round_trip(self) -> None:
        state = SimpleNodeState()
        original = {"x": 1, "y": [1, 2, 3], "z": {"k": "v"}}
        state.restore(original)
        assert state.read("x") == 1
        assert state.read("y") == [1, 2, 3]
        assert state.read("z") == {"k": "v"}

    def test_restore_shallow_copies_input(self) -> None:
        state = SimpleNodeState()
        data: dict[str, Any] = {"count": 5}
        state.restore(data)
        # Mutating the input dict after restore must not affect state.
        data["count"] = 999
        data["injected"] = True
        assert state.read("count") == 5
        assert not state.has("injected")

    def test_restore_overwrites_then_snapshot_round_trips(self) -> None:
        state = SimpleNodeState(initial={"a": 1, "b": 2})
        snap1 = state.snapshot()
        state.restore({"c": 3})
        snap2 = state.snapshot()
        assert snap1 == {"a": 1, "b": 2}
        assert snap2 == {"c": 3}


# ── SimpleNodeState: snapshot/restore round-trip ──────────────────────────


class TestSimpleNodeStateSnapshotRestoreRoundTrip:
    def test_snapshot_then_restore_round_trips(self) -> None:
        state1 = SimpleNodeState()
        state1.write("count", 42)
        state1.write("name", "alice")
        state1.write("items", [1, 2, 3])

        snap = state1.snapshot()

        state2 = SimpleNodeState()
        state2.restore(snap)

        assert state2.snapshot() == state1.snapshot()
        assert state2.read("count") == 42
        assert state2.read("name") == "alice"
        assert state2.read("items") == [1, 2, 3]

    def test_round_trip_preserves_empty_state(self) -> None:
        state1 = SimpleNodeState()
        snap = state1.snapshot()
        state2 = SimpleNodeState()
        state2.restore(snap)
        assert state2.snapshot() == {}

    def test_round_trip_is_independent_of_source(self) -> None:
        state1 = SimpleNodeState()
        state1.write("count", 7)

        snap = state1.snapshot()
        state2 = SimpleNodeState()
        state2.restore(snap)

        # Mutating state1 after the snapshot must not affect state2.
        state1.write("count", 999)
        assert state2.read("count") == 7


# ── SimpleNodeState: constructor / initial ─────────────────────────────────


class TestSimpleNodeStateConstructor:
    def test_default_empty(self) -> None:
        state = SimpleNodeState()
        assert state.snapshot() == {}

    def test_initial_dict_seeded(self) -> None:
        state = SimpleNodeState(initial={"count": 10, "name": "bob"})
        assert state.read("count") == 10
        assert state.read("name") == "bob"
        assert state.snapshot() == {"count": 10, "name": "bob"}

    def test_initial_dict_shallow_copied(self) -> None:
        original: dict[str, Any] = {"count": 5}
        state = SimpleNodeState(initial=original)
        # Mutating the input dict after construction must not affect state.
        original["count"] = 999
        original["injected"] = True
        assert state.read("count") == 5
        assert not state.has("injected")

    def test_initial_empty_dict(self) -> None:
        state = SimpleNodeState(initial={})
        assert state.snapshot() == {}

    def test_initial_allows_subsequent_writes(self) -> None:
        state = SimpleNodeState(initial={"a": 1})
        state.write("b", 2)
        assert state.read("a") == 1
        assert state.read("b") == 2


# ── SimpleNodeState: edge cases ────────────────────────────────────────────


class TestSimpleNodeStateEdgeCases:
    def test_empty_string_field_name(self) -> None:
        state = SimpleNodeState()
        state.write("", "empty_key_value")
        assert state.has("") is True
        assert state.read("") == "empty_key_value"
        assert state.snapshot() == {"": "empty_key_value"}

    def test_overwrite_with_different_type(self) -> None:
        state = SimpleNodeState()
        state.write("field", 1)
        state.write("field", "now_a_string")
        state.write("field", [1, 2])
        assert state.read("field") == [1, 2]

    def test_restore_then_overwrite(self) -> None:
        state = SimpleNodeState()
        state.restore({"a": 1})
        state.write("a", 2)
        assert state.read("a") == 2
