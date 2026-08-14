"""WorkspaceRegistry — holds WorkspaceContexts + lazily-cached resources (R).

Replaces the single-active ``_active`` value: many workspaces live concurrently,
each lazily materialized on first use and cached by resolved target path.
Generic over ``R`` so the package stays business-agnostic.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import TypeVar

from modex_agent.utils.time import now_ms
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.factory import ResourceFactory
from modex_agent.workspace.record import WorkspaceRecord

R = TypeVar("R")


class WorkspaceRegistryStore(ABC):
    """Persistence backend for known workspaces (T14 deepening).

    Enriched successor to the legacy ``RegistryStore``: stores structured
    :class:`WorkspaceRecord` metadata (workspace_id, display_name, timestamps,
    is_home, metadata_json) instead of a bare ``list[Path]``.

    Abstract methods (adapters MUST implement):
        list_workspaces(order_by, limit) -> list[WorkspaceRecord]
        upsert_workspace(record) -> None
        delete_workspace(target_path) -> None
        get_workspace(target_path) -> WorkspaceRecord | None

    Concrete legacy-compat methods (derived from the abstract ones, retained
    until T23 removes the ``RegistryStore`` alias and the ``load_known_targets``
    / ``save_known_targets`` call sites):
        load_known_targets() -> list[Path]
        save_known_targets(targets) -> None
    """

    @abstractmethod
    async def list_workspaces(
        self, order_by: str = "last_active", limit: int = 20
    ) -> list[WorkspaceRecord]:
        """Return known workspace records, sorted by ``order_by`` descending.

        Supported ``order_by`` values: ``"last_active"`` (default, most recent
        first), ``"created_at"`` (newest first).  ``limit`` caps the result
        count (default 20).
        """
        ...

    @abstractmethod
    async def upsert_workspace(self, record: WorkspaceRecord) -> None:
        """Insert or replace the record for ``record.target_path``."""
        ...

    @abstractmethod
    async def delete_workspace(self, target_path: str) -> None:
        """Remove the record keyed by ``target_path`` (no-op if absent)."""
        ...

    @abstractmethod
    async def get_workspace(self, target_path: str) -> WorkspaceRecord | None:
        """Return the record for ``target_path``, or ``None`` if absent."""
        ...

    # ------------------------------------------------------------------
    # Legacy compat — derived from the abstract methods above.
    # Retained until T23 removes the RegistryStore alias and migrates the
    # last call sites (WorkspaceRegistry.__init__ / get_or_open).
    # ------------------------------------------------------------------

    async def load_known_targets(self) -> list[Path]:
        """Deprecated: use ``list_workspaces``. Non-home target paths only."""
        records = await self.list_workspaces(order_by="last_active", limit=10_000)
        return [Path(r.target_path) for r in records if not r.is_home]

    async def save_known_targets(self, targets: list[Path]) -> None:
        """Deprecated: use ``upsert_workspace``. Full-replace semantics.

        Upserts each target (creating a minimal record for new paths,
        preserving existing metadata), then deletes non-home records whose
        target is no longer in ``targets``.
        """
        now = now_ms()
        desired = {str(Path(t).resolve()) for t in targets}
        for target in desired:
            if await self.get_workspace(target) is None:
                await self.upsert_workspace(
                    WorkspaceRecord(
                        workspace_id=str(uuid.uuid4()),
                        target_path=target,
                        display_name=None,
                        created_at=now,
                        last_active=now,
                        is_home=False,
                        metadata_json={},
                    )
                )
        for record in await self.list_workspaces(
            order_by="last_active", limit=10_000
        ):
            if record.is_home:
                continue
            resolved = str(Path(record.target_path).resolve())
            if resolved not in desired:
                await self.delete_workspace(record.target_path)


#: Deprecated alias retained until T23 removes the last import sites.
RegistryStore = WorkspaceRegistryStore


class WorkspaceRegistry[R]:
    """Holds WorkspaceContexts keyed by resolved target + lazily-cached ``R``."""

    def __init__(
        self,
        *,
        home: Path,
        data_dir_name: str,
        factory: ResourceFactory[R],
        store: WorkspaceRegistryStore,
        max_materialized: int | None = None,
    ) -> None:
        self._home: Path = Path(home).resolve()
        self._data_dir_name: str = data_dir_name
        self._factory: ResourceFactory[R] = factory
        self._store: WorkspaceRegistryStore = store
        self._max_materialized: int | None = max_materialized
        self._home_context: WorkspaceContext = WorkspaceContext.from_target(
            home, data_dir_name=data_dir_name, home=home
        )
        self._contexts: dict[Path, WorkspaceContext] = {}
        self._resources: dict[Path, R] = {}
        self._inflight: dict[Path, asyncio.Task[R]] = {}
        self._in_flight_turns: dict[Path, int] = {}
        self._lru_order: deque[Path] = deque()
        self._registry_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Load persisted workspace contexts once."""
        async with self._registry_lock:
            if self._initialized:
                return
            for target in await self._store.load_known_targets():
                resolved = Path(target).resolve()
                if resolved != self._home:
                    self._contexts[resolved] = WorkspaceContext.from_target(
                        resolved,
                        data_dir_name=self._data_dir_name,
                        home=self._home,
                    )
            self._initialized = True

    @property
    def home(self) -> Path:
        return self._home

    @property
    def home_context(self) -> WorkspaceContext:
        return self._home_context

    @property
    def factory(self) -> ResourceFactory[R]:
        return self._factory

    async def get_or_open(self, target: Path) -> WorkspaceContext:
        """Return the WorkspaceContext for ``target``, creating+registering if new.

        The home target always resolves to the implicit home context.
        """
        key = Path(target).resolve()
        if key == self._home:
            return self._home_context
        async with self._registry_lock:
            ctx = self._contexts.get(key)
            if ctx is not None:
                return ctx
            ctx = WorkspaceContext.from_target(
                target, data_dir_name=self._data_dir_name, home=self._home
            )
            now = now_ms()
            await self._store.upsert_workspace(
                WorkspaceRecord(
                    workspace_id=str(uuid.uuid4()),
                    target_path=str(key),
                    display_name=None,
                    created_at=now,
                    last_active=now,
                    is_home=False,
                    metadata_json={},
                )
            )
            self._contexts[key] = ctx
            return ctx

    def known_targets(self) -> list[Path]:
        """Return the initialized in-memory workspace targets."""
        return list(self._contexts)

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
        resources = self._resources.get(key)
        if resources is not None:
            await self._factory.evict(resources)
            self._resources.pop(key, None)
        with contextlib.suppress(ValueError):
            self._lru_order.remove(key)

    async def evict_all(self) -> bool:
        """Evict EVERY materialized resource bundle (best-effort, for shutdown).

        Used by BotService.stop() so non-home workspaces don't leak their
        broker/background tasks. Per-target errors are suppressed so one failing
        workspace cannot block teardown of the rest. Returns whether every
        materialized bundle was definitively evicted.
        """
        completed = True
        for key, resources in list(self._resources.items()):
            try:
                await self._factory.evict(resources)
            except Exception:
                completed = False
                continue
            self._resources.pop(key, None)
            with contextlib.suppress(ValueError):
                self._lru_order.remove(key)
        return completed

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
        with contextlib.suppress(ValueError):
            self._lru_order.remove(key)
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
            resources = self._resources.get(victim)
            if resources is not None:
                await self._factory.evict(resources)
                self._resources.pop(victim, None)
            with contextlib.suppress(ValueError):
                self._lru_order.remove(victim)
