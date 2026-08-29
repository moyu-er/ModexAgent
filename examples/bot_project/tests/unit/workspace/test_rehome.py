"""Tests for re-homed PoolData + BackgroundTaskRunner (build_pool_data verified at CUTOVER gate)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from bot.workspace.background import BackgroundTaskRunner
from bot.workspace.pool_data import PoolData


def test_pool_data_is_frozen_dataclass() -> None:
    import dataclasses
    import dataclasses as _dc

    assert dataclasses.is_dataclass(PoolData)
    # Frozen: assigning to a field must raise FrozenInstanceError.
    fields = {f.name for f in _dc.fields(PoolData)}
    assert "context_manager" in fields
    # The experience fields died with the experience capability's supply
    # face — PoolData inherits the snapshot's optional dir (defaults
    # None; the capability supply is the construction authority) and
    # carries no experience meta at all.
    assert "experience_meta" not in fields


async def test_background_runner_stop_is_idempotent() -> None:
    runner = BackgroundTaskRunner(pool_data={}, assembly_deps={}, default_pool_name=None)
    await runner.stop()  # never started
    await runner.start()
    await runner.stop()
    await runner.stop()  # second stop is a no-op
    assert runner.tasks == []


async def test_background_runner_dream_loop_starts_and_stops() -> None:
    """Regression: start() must launch the DreamEngine background loop when a
    dream engine is present, and stop() must cancel it.

    Re-targeted from the old BotService-level dream lifecycle (the loop now
    lives on BackgroundTaskRunner, re-homed from Workspace). The real build
    path (``_maybe_build_dream`` from pool_data) is exercised at CUTOVER; here
    we inject the engine directly to test the loop wiring in isolation.
    """
    from types import SimpleNamespace

    runner = BackgroundTaskRunner(pool_data={}, assembly_deps={}, default_pool_name=None)
    # No default pool -> _maybe_build_dream left dream_engine None; inject one.
    runner.dream_engine = SimpleNamespace(scan_all=AsyncMock(return_value=[]))
    runner._dream_interval = 3600  # keep the loop sleeping so scan_all never fires

    await runner.start()
    # Dream loop only — the curator loops moved to the experience
    # capability supply (pool assembly starts them; pool teardown stops
    # them; SPEC §8.3 D4), so the runner owns exactly one task.
    assert len(runner.tasks) == 1
    assert runner.tasks[0].get_name() == "workspace-dream"

    await runner.stop()
    assert runner.tasks == []
