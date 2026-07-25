"""Tests for WriteConflictDetector ABC + GenerationWriteTracker default impl.

Per ADR-0034 D18: generation-based conflict detection for LastValue fields
under continuous scheduling. A generation = all instances that forked the
same main_state snapshot. Two same-generation instances writing the same
LastValue field = conflict (InvalidUpdateError). Cross-generation writes
to the same field = sequential overwrite (no conflict).

Covers:

- `WriteConflictDetector` ABC is abstract (cannot instantiate directly).
- `GenerationWriteTracker` is concrete (no abstract methods).
- `register` + `commit` + `advance` + `complete` lifecycle.
- Same-generation conflict: two instances with same fork_version write
  the same LastValue field -> InvalidUpdateError.
- Cross-generation no conflict: instance with fork_version=0 writes a
  field, advance(), instance with fork_version=1 writes the same field
  -> no error.
- Cleanup: complete() drops generation writer count to 0 -> generation
  entry deleted (subsequent commit to that version is a no-op).
- reset(): clears all state.
- current_version property.
- Multiple fields in one commit (some conflict, some don't).
- commit on unknown generation is a no-op (no raise).
- complete on unknown generation is a no-op.
"""

from __future__ import annotations

import pytest

from modex_graph import (
    GenerationWriteTracker,
    InvalidUpdateError,
    WriteConflictDetector,
)

# ── ABC structure ──────────────────────────────────────────────────────────


class TestWriteConflictDetectorABC:
    def test_is_abstract(self) -> None:
        from abc import ABC

        assert issubclass(WriteConflictDetector, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            WriteConflictDetector()  # type: ignore[abstract]

    def test_five_abstract_methods(self) -> None:
        expected = {"register", "commit", "complete", "advance", "reset"}
        assert set(WriteConflictDetector.__abstractmethods__) == expected


class TestGenerationWriteTrackerIsConcrete:
    def test_inherits_abc(self) -> None:
        assert issubclass(GenerationWriteTracker, WriteConflictDetector)

    def test_no_abstract_methods(self) -> None:
        assert len(GenerationWriteTracker.__abstractmethods__) == 0

    def test_can_instantiate(self) -> None:
        tracker = GenerationWriteTracker()
        assert isinstance(tracker, WriteConflictDetector)

    def test_has_slots(self) -> None:
        assert hasattr(GenerationWriteTracker, "__slots__")
        assert set(GenerationWriteTracker.__slots__) == {
            "_current_version",
            "_generations",
        }


# ── current_version property ───────────────────────────────────────────────


class TestCurrentVersion:
    def test_starts_at_zero(self) -> None:
        tracker = GenerationWriteTracker()
        assert tracker.current_version == 0

    def test_advance_increments(self) -> None:
        tracker = GenerationWriteTracker()
        assert tracker.advance() == 1
        assert tracker.current_version == 1
        assert tracker.advance() == 2
        assert tracker.current_version == 2

    def test_reset_sets_back_to_zero(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.advance()
        tracker.advance()
        assert tracker.current_version == 2
        tracker.reset()
        assert tracker.current_version == 0


# ── register + commit + complete lifecycle ─────────────────────────────────


class TestLifecycle:
    def test_register_commit_complete_no_raise(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.commit(0, ["count"])
        tracker.advance()
        tracker.complete(0)
        assert tracker.current_version == 1

    def test_commit_on_unregistered_generation_is_noop(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.commit(999, ["count"])

    def test_complete_on_unregistered_generation_is_noop(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.complete(999)

    def test_commit_empty_fields_is_noop(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.commit(0, [])
        tracker.complete(0)


# ── Same-generation conflict ───────────────────────────────────────────────


class TestSameGenerationConflict:
    """Two instances with same fork_version writing same field -> conflict."""

    def test_two_writers_same_field_raises(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.register(0)

        tracker.commit(0, ["count"])
        tracker.advance()
        tracker.complete(0)

        with pytest.raises(InvalidUpdateError, match="count"):
            tracker.commit(0, ["count"])

    def test_conflict_error_mentions_generation(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(5)
        tracker.register(5)

        tracker.commit(5, ["name"])
        tracker.advance()
        tracker.complete(5)

        with pytest.raises(InvalidUpdateError, match="generation 5"):
            tracker.commit(5, ["name"])

    def test_two_writers_different_fields_no_conflict(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.register(0)

        tracker.commit(0, ["count"])
        tracker.advance()
        tracker.complete(0)

        tracker.commit(0, ["name"])
        tracker.advance()
        tracker.complete(0)

    def test_same_writer_writes_same_field_twice_raises(self) -> None:
        """Even the same writer committing the same field twice in one
        generation is a conflict (defensive — shouldn't happen in practice
        but the detector doesn't special-case it)."""
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.commit(0, ["count"])
        with pytest.raises(InvalidUpdateError):
            tracker.commit(0, ["count"])


# ── Cross-generation no conflict ───────────────────────────────────────────


class TestCrossGenerationNoConflict:
    """Instance with fork_version=0 writes field, advance(), instance with
    fork_version=1 writes same field -> no error (sequential overwrite)."""

    def test_cross_generation_same_field_no_raise(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.commit(0, ["count"])
        tracker.advance()
        tracker.complete(0)

        tracker.register(1)
        tracker.commit(1, ["count"])
        tracker.advance()
        tracker.complete(1)

    def test_cross_generation_after_multiple_advances(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.commit(0, ["count"])
        tracker.advance()
        tracker.complete(0)

        tracker.advance()

        tracker.register(2)
        tracker.commit(2, ["count"])
        tracker.advance()
        tracker.complete(2)


# ── Cleanup: complete decrements count but does NOT delete ─────────────────


class TestCleanup:
    def test_complete_does_not_delete_generation(self) -> None:
        """After complete drops count to 0, the generation entry is NOT
        deleted — its written_fields persist for cross-generation conflict
        detection. A subsequent commit to that version still raises."""
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.commit(0, ["count"])
        tracker.advance()
        tracker.complete(0)

        with pytest.raises(InvalidUpdateError, match="count"):
            tracker.commit(0, ["count"])

    def test_partial_complete_keeps_generation(self) -> None:
        """Two writers in gen 0. complete one -> count=1, gen still alive.
        The second writer still conflicts on already-written fields."""
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.register(0)

        tracker.commit(0, ["count"])
        tracker.advance()
        tracker.complete(0)

        with pytest.raises(InvalidUpdateError):
            tracker.commit(0, ["count"])

        tracker.complete(0)
        with pytest.raises(InvalidUpdateError):
            tracker.commit(0, ["count"])


# ── Cross-generation concurrent conflict (C2 fix) ──────────────────────────


class TestCrossGenerationConcurrentConflict:
    """When a new generation registers while an older generation is still
    in-flight (pending_count > 0), they are bidirectionally concurrent.
    A commit from either side detects a conflict if both wrote the same
    LastValue field — even if one completes before the other commits.
    """

    def test_new_gen_conflicts_with_in_flight_old_gen(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.advance()
        tracker.register(1)
        tracker.commit(1, ["count"])
        tracker.advance()

        with pytest.raises(InvalidUpdateError, match="count"):
            tracker.commit(0, ["count"])

    def test_old_gen_conflicts_with_in_flight_new_gen(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.advance()
        tracker.register(1)
        tracker.commit(0, ["count"])
        tracker.advance()

        with pytest.raises(InvalidUpdateError, match="count"):
            tracker.commit(1, ["count"])

    def test_completed_old_gen_not_concurrent_with_new_gen(self) -> None:
        """If the old generation's pending_count is 0 when the new gen
        registers, they are NOT concurrent — sequential overwrite is OK."""
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.commit(0, ["count"])
        tracker.advance()
        tracker.complete(0)

        tracker.register(1)
        tracker.commit(1, ["count"])
        tracker.advance()
        tracker.complete(1)


# ── reset ──────────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_clears_generations(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.register(0)
        tracker.commit(0, ["count"])
        tracker.advance()

        tracker.reset()

        assert tracker.current_version == 0
        # After reset, gen 0 is gone -> commit is a no-op (no raise).
        tracker.commit(0, ["count"])

    def test_reset_after_complete(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.commit(0, ["count"])
        tracker.advance()
        tracker.complete(0)
        assert tracker.current_version == 1

        tracker.reset()
        assert tracker.current_version == 0

    def test_reset_idempotent(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.reset()
        tracker.reset()
        assert tracker.current_version == 0


# ── Multiple fields in one commit ──────────────────────────────────────────


class TestMultipleFieldsInCommit:
    def test_multiple_non_conflicting_fields_all_added(self) -> None:
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.register(0)

        tracker.commit(0, ["count", "name"])
        tracker.advance()
        tracker.complete(0)

        # Second writer: "items" is new, but "count" and "name" conflict.
        with pytest.raises(InvalidUpdateError, match="count"):
            tracker.commit(0, ["count", "name", "items"])

    def test_commit_adds_fields_incrementally(self) -> None:
        """First commit adds count+name, second commit (different writer)
        can write a new field but conflicts on any already-written one."""
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.register(0)

        tracker.commit(0, ["count", "name"])
        tracker.advance()
        tracker.complete(0)

        # "items" is new -> no conflict on that field alone.
        tracker.commit(0, ["items"])
        tracker.advance()
        tracker.complete(0)

    def test_conflict_on_second_field_in_commit(self) -> None:
        """Fields are checked in order. If the first field is new but the
        second conflicts, the first is added before the raise."""
        tracker = GenerationWriteTracker()
        tracker.register(0)
        tracker.register(0)

        tracker.commit(0, ["count"])
        tracker.advance()
        tracker.complete(0)

        # "name" is new (added), "count" conflicts (raises).
        with pytest.raises(InvalidUpdateError, match="count"):
            tracker.commit(0, ["name", "count"])


# ── Full scheduler-like lifecycle simulation ───────────────────────────────


class TestSchedulerLikeLifecycle:
    """Simulate the exact sequence ParallelScheduler uses:
    register -> commit -> apply_state_update -> advance -> complete."""

    def test_two_concurrent_instances_one_conflicts(self) -> None:
        tracker = GenerationWriteTracker()

        # Two instances fork the same snapshot (gen 0).
        tracker.register(0)
        tracker.register(0)

        # Instance 1 writes "count" -> OK.
        tracker.commit(0, ["count"])
        # (apply_state_update would happen here)
        tracker.advance()
        tracker.complete(0)

        # Instance 2 writes "count" -> conflict.
        with pytest.raises(InvalidUpdateError):
            tracker.commit(0, ["count"])

    def test_three_generations_sequential_no_conflict(self) -> None:
        tracker = GenerationWriteTracker()

        for gen in range(3):
            tracker.register(gen)
            tracker.commit(gen, ["count"])
            tracker.advance()
            tracker.complete(gen)

        assert tracker.current_version == 3
