"""Tests for ``StaleInstanceSweeper``.

Covers:
- Running with alive executor → not swept.
- Running with dead executor → swept to CRASHED.
- Running with NULL executor (no attrs key) → swept to CRASHED.
- Running with NULL executor (explicit None value) → swept to CRASHED.
- Terminal instances (COMPLETED/FAILED/CRASHED/STOPPED) → not touched.
- Mixed batch: alive skipped, dead + NULL swept.
- Sweeper does NOT trigger recovery (status only — no orchestrator ref).
- ``start_sweeper_loop`` runs periodically and can be cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from bot.service.stale_instance_sweeper import StaleInstanceSweeper, start_sweeper_loop

from modex_agent.runtime.constants import EXECUTOR_PROCESS_ID_KEY
from modex_agent.runtime.process_registry import ProcessRegistry
from modex_graph import (
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphMetadata,
    InMemoryGraphInstanceStore,
)

# -- Helpers -----------------------------------------------------------------


class _FakeRegistry(ProcessRegistry):
    """Minimal process registry for tests."""

    def __init__(self, alive_ids: set[int]) -> None:
        self._alive = set(alive_ids)

    def alive_process_ids(self) -> set[int]:
        return set(self._alive)

    def register(self, process_id: int) -> None:
        self._alive.add(process_id)

    def unregister(self, process_id: int) -> None:
        self._alive.discard(process_id)


def _make_metadata(
    gid: int,
    *,
    status: GraphInstanceStatus,
    attrs: dict[str, int | str | None] | None = None,
) -> GraphMetadata:
    return GraphMetadata(
        graph_instance_id=gid,
        spec_id=1,
        parent_instance_id=None,
        parent_node=None,
        status=status,
        attrs=attrs if attrs is not None else {},
    )


def _status(store: GraphInstanceStore, gid: int) -> GraphInstanceStatus:
    meta = store.load(gid)
    assert meta is not None, f"Instance {gid} not found"
    return meta.status


# -- Alive executor → not swept ----------------------------------------------


class TestSweepAlive:
    async def test_running_with_alive_executor_not_swept(self) -> None:
        store = InMemoryGraphInstanceStore()
        alive_pid = 1001
        store.save(
            _make_metadata(
                1,
                status=GraphInstanceStatus.RUNNING,
                attrs={EXECUTOR_PROCESS_ID_KEY: alive_pid},
            )
        )
        sweeper = StaleInstanceSweeper(store, _FakeRegistry({alive_pid}))

        swept = await sweeper.sweep()

        assert swept == []
        assert _status(store, 1) is GraphInstanceStatus.RUNNING


# -- Dead executor → swept to CRASHED ----------------------------------------


class TestSweepDead:
    async def test_running_with_dead_executor_swept_to_crashed(self) -> None:
        store = InMemoryGraphInstanceStore()
        dead_pid = 2002
        store.save(
            _make_metadata(
                2,
                status=GraphInstanceStatus.RUNNING,
                attrs={EXECUTOR_PROCESS_ID_KEY: dead_pid},
            )
        )
        sweeper = StaleInstanceSweeper(store, _FakeRegistry({9999}))

        swept = await sweeper.sweep()

        assert swept == [2]
        assert _status(store, 2) is GraphInstanceStatus.CRASHED


# -- NULL executor → swept to CRASHED ----------------------------------------


class TestSweepNull:
    async def test_running_with_no_attrs_key_swept_to_crashed(self) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(_make_metadata(3, status=GraphInstanceStatus.RUNNING))
        sweeper = StaleInstanceSweeper(store, _FakeRegistry({9999}))

        swept = await sweeper.sweep()

        assert swept == [3]
        assert _status(store, 3) is GraphInstanceStatus.CRASHED

    async def test_running_with_explicit_none_executor_swept_to_crashed(self) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(
            _make_metadata(
                4,
                status=GraphInstanceStatus.RUNNING,
                attrs={EXECUTOR_PROCESS_ID_KEY: None},
            )
        )
        sweeper = StaleInstanceSweeper(store, _FakeRegistry({9999}))

        swept = await sweeper.sweep()

        assert swept == [4]
        assert _status(store, 4) is GraphInstanceStatus.CRASHED


# -- Terminal instances → not touched ----------------------------------------


class TestSweepTerminal:
    @pytest.mark.parametrize(
        "status",
        [
            GraphInstanceStatus.COMPLETED,
            GraphInstanceStatus.FAILED,
            GraphInstanceStatus.CRASHED,
            GraphInstanceStatus.STOPPED,
        ],
    )
    async def test_terminal_instances_not_swept(self, status: GraphInstanceStatus) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(
            _make_metadata(
                5,
                status=status,
                attrs={EXECUTOR_PROCESS_ID_KEY: 9999},  # dead, but terminal
            )
        )
        sweeper = StaleInstanceSweeper(store, _FakeRegistry(set()))

        swept = await sweeper.sweep()

        assert swept == []
        assert _status(store, 5) is status


# -- Mixed batch -------------------------------------------------------------


class TestSweepMixed:
    async def test_mixed_batch(self) -> None:
        store = InMemoryGraphInstanceStore()
        alive_pid = 3003
        store.save(
            _make_metadata(
                10,
                status=GraphInstanceStatus.RUNNING,
                attrs={EXECUTOR_PROCESS_ID_KEY: alive_pid},
            )
        )
        store.save(
            _make_metadata(
                11,
                status=GraphInstanceStatus.RUNNING,
                attrs={EXECUTOR_PROCESS_ID_KEY: 5555},  # dead
            )
        )
        store.save(_make_metadata(12, status=GraphInstanceStatus.RUNNING))  # NULL
        store.save(
            _make_metadata(
                13,
                status=GraphInstanceStatus.COMPLETED,
                attrs={EXECUTOR_PROCESS_ID_KEY: 5555},  # terminal, not scanned
            )
        )
        sweeper = StaleInstanceSweeper(store, _FakeRegistry({alive_pid}))

        swept = await sweeper.sweep()

        assert set(swept) == {11, 12}
        assert _status(store, 10) is GraphInstanceStatus.RUNNING
        assert _status(store, 11) is GraphInstanceStatus.CRASHED
        assert _status(store, 12) is GraphInstanceStatus.CRASHED
        assert _status(store, 13) is GraphInstanceStatus.COMPLETED


# -- No recovery (status only) ----------------------------------------------


class TestNoRecovery:
    async def test_sweeper_has_no_orchestrator_reference(self) -> None:
        """The sweeper only holds instance_store + process_registry — no
        orchestrator, so it cannot trigger recovery."""
        store = InMemoryGraphInstanceStore()
        sweeper = StaleInstanceSweeper(store, _FakeRegistry(set()))

        assert not hasattr(sweeper, "_orchestrator")
        assert not hasattr(sweeper, "_recovery_service")

    async def test_swept_instance_stays_crashed_not_re_run(self) -> None:
        """After sweeping, the instance is CRASHED and stays CRASHED —
        the sweeper does not re-run it (recovery is explicit)."""
        store = InMemoryGraphInstanceStore()
        store.save(
            _make_metadata(
                20,
                status=GraphInstanceStatus.RUNNING,
                attrs={EXECUTOR_PROCESS_ID_KEY: 7777},
            )
        )
        sweeper = StaleInstanceSweeper(store, _FakeRegistry(set()))

        await sweeper.sweep()
        assert _status(store, 20) is GraphInstanceStatus.CRASHED

        # A second sweep finds no RUNNING instances (already CRASHED)
        swept_again = await sweeper.sweep()
        assert swept_again == []
        assert _status(store, 20) is GraphInstanceStatus.CRASHED


# -- Empty scan --------------------------------------------------------------


class TestSweepEmpty:
    async def test_no_running_instances_returns_empty(self) -> None:
        store = InMemoryGraphInstanceStore()
        sweeper = StaleInstanceSweeper(store, _FakeRegistry({9999}))

        swept = await sweeper.sweep()

        assert swept == []


@pytest.mark.parametrize("status", [GraphInstanceStatus.PAUSING, GraphInstanceStatus.STOPPING])
@pytest.mark.parametrize("alive", [False, True])
async def test_transitional_instances_use_the_same_executor_classifier(
    status: GraphInstanceStatus, alive: bool,
) -> None:
    store = InMemoryGraphInstanceStore()
    store.save(_make_metadata(40, status=status, attrs={EXECUTOR_PROCESS_ID_KEY: 100}))
    sweeper = StaleInstanceSweeper(store, _FakeRegistry({100} if alive else set()))
    assert await sweeper.sweep() == ([] if alive else [40])
    assert _status(store, 40) is (status if alive else GraphInstanceStatus.CRASHED)


# -- start_sweeper_loop ------------------------------------------------------


class TestStartSweeperLoop:
    async def test_loop_sweeps_periodically_then_cancels(self) -> None:
        store = InMemoryGraphInstanceStore()
        store.save(
            _make_metadata(
                30,
                status=GraphInstanceStatus.RUNNING,
                attrs={EXECUTOR_PROCESS_ID_KEY: 8888},  # dead
            )
        )
        sweeper = StaleInstanceSweeper(store, _FakeRegistry(set()))
        task = start_sweeper_loop(sweeper, interval_seconds=0.05)

        # Wait long enough for at least one sweep
        await asyncio.sleep(0.15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert _status(store, 30) is GraphInstanceStatus.CRASHED

    async def test_loop_continues_after_sweep_error(self) -> None:
        """A sweep that raises should not kill the loop."""

        class _ExplodingStore(InMemoryGraphInstanceStore):
            call_count = 0

            def load_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]:
                _ExplodingStore.call_count += 1
                raise RuntimeError("boom")

        store = _ExplodingStore()
        sweeper = StaleInstanceSweeper(store, _FakeRegistry(set()))
        task = start_sweeper_loop(sweeper, interval_seconds=0.02)

        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert _ExplodingStore.call_count > 0
