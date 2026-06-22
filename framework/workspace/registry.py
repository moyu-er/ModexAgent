"""WorkspaceRegistry — holds WorkspaceContexts + lazily-cached resources (R).

Replaces the single-active ``_active`` value: many workspaces live concurrently,
each lazily materialized on first use and cached by resolved target path.
Generic over ``R`` so the package stays business-agnostic.
"""
from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Generic, TypeVar

from framework.workspace.context import WorkspaceContext
from framework.workspace.factory import ResourceFactory

R = TypeVar("R")


class RegistryStore(ABC):
    """Persistence backend for the set of known (non-home) workspace targets."""

    @abstractmethod
    def load_known_targets(self) -> list[Path]:
        """Return previously-registered non-home workspace target paths."""

    @abstractmethod
    def save_known_targets(self, targets: list[Path]) -> None:
        """Persist the current set of non-home workspace target paths."""


class InMemoryRegistryStore(RegistryStore):
    """In-memory RegistryStore for tests."""

    def __init__(self) -> None:
        self._targets: list[Path] = []

    def load_known_targets(self) -> list[Path]:
        return list(self._targets)

    def save_known_targets(self, targets: list[Path]) -> None:
        self._targets = list(targets)


class WorkspaceRegistry(Generic[R]):
    """Holds WorkspaceContexts keyed by resolved target + lazily-cached ``R``."""

    def __init__(
        self,
        *,
        home: Path,
        data_dir_name: str,
        factory: ResourceFactory[R],
        store: RegistryStore,
        max_materialized: int | None = None,
    ) -> None:
        self._home: Path = Path(home).resolve()
        self._data_dir_name: str = data_dir_name
        self._factory: ResourceFactory[R] = factory
        self._store: RegistryStore = store
        self._max_materialized: int | None = max_materialized
        self._home_context: WorkspaceContext = WorkspaceContext.from_target(
            home, data_dir_name=data_dir_name, home=home
        )
        self._contexts: dict[Path, WorkspaceContext] = {}
        self._resources: dict[Path, R] = {}
        self._inflight: dict[Path, asyncio.Task[R]] = {}
        self._in_flight_turns: dict[Path, int] = {}
        self._lru_order: deque[Path] = deque()
        for target in store.load_known_targets():
            resolved = Path(target).resolve()
            if resolved != self._home:
                self._contexts[resolved] = WorkspaceContext.from_target(
                    resolved, data_dir_name=data_dir_name, home=self._home
                )

    @property
    def home(self) -> Path:
        return self._home

    @property
    def home_context(self) -> WorkspaceContext:
        return self._home_context

    @property
    def factory(self) -> ResourceFactory[R]:
        return self._factory

    def get_or_open(self, target: Path) -> WorkspaceContext:
        """Return the WorkspaceContext for ``target``, creating+registering if new.

        The home target always resolves to the implicit home context.
        """
        key = Path(target).resolve()
        if key == self._home:
            return self._home_context
        ctx = self._contexts.get(key)
        if ctx is None:
            ctx = WorkspaceContext.from_target(
                target, data_dir_name=self._data_dir_name, home=self._home
            )
            self._contexts[key] = ctx
            self._store.save_known_targets(
                [t for t in self._contexts if t != self._home]
            )
        return ctx

    async def materialize(self, ctx: WorkspaceContext) -> R:
        """Lazily build + cache the resource bundle for ``ctx`` (cached on target).

        Concurrent materialize() calls for the SAME target share a single
        in-flight task so the factory runs once and no resource bundle is
        orphaned (which would leak its broker/background tasks).
        """
        key = Path(ctx.target).resolve()
        cached = self._resources.get(key)
        if cached is not None:
            self._touch_lru(key)
            return cached
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._materialize_once(ctx, key))
            self._inflight[key] = task
        return await task

    async def _materialize_once(self, ctx: WorkspaceContext, key: Path) -> R:
        try:
            resources = await self._factory.materialize(ctx)
        finally:
            # Clear the in-flight marker on success OR failure: a failed
            # materialize must be retryable, not cached as a permanent error.
            self._inflight.pop(key, None)
        self._resources[key] = resources
        self._touch_lru(key)
        await self._enforce_cap(protected=key)
        return resources

    async def evict_and_release(self, target: Path) -> None:
        """Drop the cached resource bundle for ``target`` and evict it (memory only)."""
        key = Path(target).resolve()
        resources = self._resources.pop(key, None)
        if resources is not None:
            await self._factory.evict(resources)
        try:
            self._lru_order.remove(key)
        except ValueError:
            pass

    async def evict_all(self) -> None:
        """Evict EVERY materialized resource bundle (best-effort, for shutdown).

        Used by BotService.stop() so non-home workspaces don't leak their
        broker/background tasks. Per-target errors are suppressed so one failing
        workspace cannot block teardown of the rest.
        """
        for key in list(self._resources.keys()):
            resources = self._resources.pop(key, None)
            try:
                self._lru_order.remove(key)
            except ValueError:
                pass
            if resources is not None:
                with contextlib.suppress(BaseException):
                    await self._factory.evict(resources)

    def materialized_count(self) -> int:
        return len(self._resources)

    def iter_materialized_resources(self) -> Iterator[R]:
        """Yield all currently materialized resource bundles."""
        yield from self._resources.values()

    def begin_turn(self, target: Path) -> None:
        """Mark a turn as in-flight on ``target``'s workspace (protected from eviction)."""
        key = Path(target).resolve()
        self._in_flight_turns[key] = self._in_flight_turns.get(key, 0) + 1

    def end_turn(self, target: Path) -> None:
        """Mark a turn on ``target``'s workspace as complete."""
        key = Path(target).resolve()
        count = self._in_flight_turns.get(key, 0)
        if count <= 1:
            self._in_flight_turns.pop(key, None)
        else:
            self._in_flight_turns[key] = count - 1

    def _touch_lru(self, target: Path) -> None:
        """Move ``target`` to the most-recently-used position."""
        key = Path(target).resolve()
        try:
            self._lru_order.remove(key)
        except ValueError:
            pass
        self._lru_order.append(key)

    def _oldest_evictable(self, *, protected: Path | None) -> Path | None:
        """Oldest materialized target that is neither in-flight nor ``protected``."""
        for key in self._lru_order:  # oldest first
            if self._in_flight_turns.get(key, 0) > 0:
                continue
            if protected is not None and key == protected:
                continue
            return key
        return None

    async def _enforce_cap(self, *, protected: Path | None = None) -> None:
        """Evict oldest EVICTABLE resources until within ``max_materialized``.

        Never evicts a workspace with an in-flight turn (would corrupt the
        running turn) or the ``protected`` target (the one just materialized
        for the caller about to use it). If nothing is evictable, the cap is
        exceeded transiently until a turn ends — preferable to corrupting a
        running turn.
        """
        if self._max_materialized is None:
            return
        while len(self._resources) > self._max_materialized:
            victim = self._oldest_evictable(protected=protected)
            if victim is None:
                break
            resources = self._resources.pop(victim, None)
            try:
                self._lru_order.remove(victim)
            except ValueError:
                pass
            if resources is not None:
                await self._factory.evict(resources)
