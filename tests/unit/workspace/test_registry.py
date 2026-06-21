"""Tests for framework.workspace.registry.WorkspaceRegistry (stub R + stub factory)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import pytest

from framework.workspace.registry import InMemoryRegistryStore, WorkspaceRegistry
from ._stubs import StubFactory, StubResources


@pytest.fixture
def registry(tmp_path: Path) -> WorkspaceRegistry[StubResources]:
    home = tmp_path / "proj"
    home.mkdir()
    return WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=StubFactory(),
        store=InMemoryRegistryStore(),
    )


async def test_home_context_always_present(
    registry: WorkspaceRegistry[StubResources],
) -> None:
    assert registry.home_context.is_home is True


async def test_get_or_open_registers_non_home(
    registry: WorkspaceRegistry[StubResources], tmp_path: Path
) -> None:
    target = tmp_path / "wsB"
    target.mkdir()
    ctx = registry.get_or_open(target)
    assert ctx.is_home is False
    assert registry.get_or_open(target) is ctx  # same context on repeat


async def test_materialize_is_lazy_and_cached(
    registry: WorkspaceRegistry[StubResources], tmp_path: Path
) -> None:
    target = tmp_path / "wsB"
    target.mkdir()
    ctx = registry.get_or_open(target)
    assert registry.factory.calls == []  # not materialized at registration
    r1 = await registry.materialize(ctx)
    r2 = await registry.materialize(ctx)
    assert r1 is r2  # cached
    assert registry.factory.calls == [ctx.target]  # built exactly once


async def test_concurrent_materialize_same_target_dedups(tmp_path: Path) -> None:
    """Two concurrent materialize() of the same target share ONE factory call.

    Without a per-target in-flight guard, both callers miss the cache and each
    runs the factory, orphaning one resource bundle (leaked broker/tasks).
    """

    class _SlowFactory(StubFactory):
        async def materialize(self, ctx):  # type: ignore[override]
            await asyncio.sleep(0)  # yield so the second caller overlaps
            return await super().materialize(ctx)

    home = tmp_path / "proj"
    home.mkdir()
    factory = _SlowFactory()
    registry = WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=factory,
        store=InMemoryRegistryStore(),
    )
    target = tmp_path / "wsB"
    target.mkdir()
    ctx = registry.get_or_open(target)
    r1, r2 = await asyncio.gather(
        registry.materialize(ctx), registry.materialize(ctx)
    )
    assert r1 is r2  # shared materialization
    assert factory.calls == [ctx.target]  # factory invoked exactly once


async def test_evict_releases_cache_and_rematerializes_fresh(
    registry: WorkspaceRegistry[StubResources], tmp_path: Path
) -> None:
    target = tmp_path / "wsB"
    target.mkdir()
    ctx = registry.get_or_open(target)
    built = await registry.materialize(ctx)
    await registry.evict_and_release(ctx.target)
    assert registry.materialized_count() == 0
    assert built.evicted is True  # factory.evict was called
    rebuilt = await registry.materialize(ctx)
    assert rebuilt is not built
    assert rebuilt.evicted is False


async def test_no_eviction_when_under_cap(tmp_path: Path) -> None:
    home = tmp_path / "proj"
    home.mkdir()
    registry = WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=StubFactory(),
        store=InMemoryRegistryStore(),
        max_materialized=3,
    )
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    ctx1 = registry.get_or_open(ws1)
    ctx2 = registry.get_or_open(ws2)
    await registry.materialize(ctx1)
    await registry.materialize(ctx2)
    assert registry.materialized_count() == 2
    assert registry.factory.calls == [ctx1.target, ctx2.target]


async def test_eviction_of_oldest_when_over_cap(tmp_path: Path) -> None:
    home = tmp_path / "proj"
    home.mkdir()
    factory = StubFactory()
    registry = WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=factory,
        store=InMemoryRegistryStore(),
        max_materialized=2,
    )
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    ws3 = tmp_path / "ws3"
    ws3.mkdir()
    ctx1 = registry.get_or_open(ws1)
    ctx2 = registry.get_or_open(ws2)
    ctx3 = registry.get_or_open(ws3)
    r1 = await registry.materialize(ctx1)
    r2 = await registry.materialize(ctx2)
    r3 = await registry.materialize(ctx3)
    assert registry.materialized_count() == 2
    assert r1.evicted is True
    assert r2.evicted is False
    assert r3.evicted is False
    assert factory.calls == [ctx1.target, ctx2.target, ctx3.target]


async def test_rematerialization_after_eviction(tmp_path: Path) -> None:
    home = tmp_path / "proj"
    home.mkdir()
    factory = StubFactory()
    registry = WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=factory,
        store=InMemoryRegistryStore(),
        max_materialized=1,
    )
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    ctx1 = registry.get_or_open(ws1)
    ctx2 = registry.get_or_open(ws2)
    r1_first = await registry.materialize(ctx1)
    await registry.materialize(ctx2)  # evicts r1
    assert r1_first.evicted is True
    r1_second = await registry.materialize(ctx1)
    assert r1_second is not r1_first
    assert r1_second.evicted is False
    assert factory.calls == [ctx1.target, ctx2.target, ctx1.target]


async def test_lru_order_updates_on_access(tmp_path: Path) -> None:
    home = tmp_path / "proj"
    home.mkdir()
    factory = StubFactory()
    registry = WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=factory,
        store=InMemoryRegistryStore(),
        max_materialized=2,
    )
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    ws3 = tmp_path / "ws3"
    ws3.mkdir()
    ctx1 = registry.get_or_open(ws1)
    ctx2 = registry.get_or_open(ws2)
    ctx3 = registry.get_or_open(ws3)
    r1 = await registry.materialize(ctx1)
    r2 = await registry.materialize(ctx2)
    # Access r1 to bump it to most-recently-used
    _ = await registry.materialize(ctx1)
    # Now materialize ws3; should evict r2 (oldest), not r1
    r3 = await registry.materialize(ctx3)
    assert r1.evicted is False
    assert r2.evicted is True
    assert r3.evicted is False
    assert registry.materialized_count() == 2


async def test_evict_all_evicts_every_materialized(
    registry: WorkspaceRegistry[StubResources], tmp_path: Path
) -> None:
    """evict_all() tears down EVERY materialized workspace, not just one.

    BotService.stop() calls this on shutdown so non-home workspaces don't leak
    their broker/background tasks.
    """
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    r1 = await registry.materialize(registry.get_or_open(ws1))
    r2 = await registry.materialize(registry.get_or_open(ws2))
    await registry.evict_all()
    assert r1.evicted is True
    assert r2.evicted is True
    assert registry.materialized_count() == 0


async def test_in_flight_workspace_protected_from_eviction(tmp_path: Path) -> None:
    """An in-flight turn's workspace is never evicted, even when it is the oldest.

    Without protection the oldest (ws1) would be evicted on the ws3 materialize;
    instead the next-oldest evictable (ws2) is evicted. Once ws1's turn ends it
    becomes evictable again.
    """
    home = tmp_path / "proj"
    home.mkdir()
    factory = StubFactory()
    registry = WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=factory,
        store=InMemoryRegistryStore(),
        max_materialized=2,
    )
    ws1, ws2, ws3, ws4 = (tmp_path / n for n in ("ws1", "ws2", "ws3", "ws4"))
    for ws in (ws1, ws2, ws3, ws4):
        ws.mkdir()
    ctx1 = registry.get_or_open(ws1)
    ctx2 = registry.get_or_open(ws2)
    ctx3 = registry.get_or_open(ws3)
    ctx4 = registry.get_or_open(ws4)
    r1 = await registry.materialize(ctx1)
    registry.begin_turn(ctx1.target)  # active turn on ws1
    r2 = await registry.materialize(ctx2)
    await registry.materialize(ctx3)  # over cap (3 > 2); ws1 oldest but in-flight
    assert r1.evicted is False  # in-flight workspace NOT evicted
    assert r2.evicted is True  # next-oldest evictable evicted instead
    assert registry.materialized_count() == 2

    registry.end_turn(ctx1.target)  # ws1 idle now
    await registry.materialize(ctx4)  # enforces cap; ws1 now evictable (oldest)
    assert r1.evicted is True


async def test_contexts_retained_after_eviction(tmp_path: Path) -> None:
    home = tmp_path / "proj"
    home.mkdir()
    registry = WorkspaceRegistry(
        home=home,
        data_dir_name=".modex",
        factory=StubFactory(),
        store=InMemoryRegistryStore(),
        max_materialized=1,
    )
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    ctx1 = registry.get_or_open(ws1)
    await registry.materialize(ctx1)
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    ctx2 = registry.get_or_open(ws2)
    await registry.materialize(ctx2)  # evicts ws1 resource
    # Contexts should still be retrievable
    assert registry.get_or_open(ws1) is ctx1
    assert registry.get_or_open(ws2) is ctx2
