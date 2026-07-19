"""Crash-safe session garbage collection (ADR-0018).

A bot-side collector that deletes a conversation's full cascade (root + every
subagent descendant via ``parent_session_id``) and all nine per-session artifact
types. Crash recoverability is the first constraint: deletion progress is fully
reconstructable from disk (the session-index graph) after a restart, with no
in-memory closure collected up front. See ADR-0018 and the session-lifecycle
glossary in CONTEXT.md.

T17: The per-session artifact cleanup is delegated to
:class:`modex_agent.core.cleanup.SessionArtifactCleaner`.  The artifact list
dropped from ten to nine (``fork_contexts`` removed, aligning with T18 which
removes fork XML file writing).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from bot.scope import BotRecordScope
from modex_agent.core.cleanup import (
    DefaultSessionArtifactCleaner,
    SessionCleanupResult,
    session_artifact_paths,
)
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_cleanup import MissingSessionScopeError
from modex_agent.core.session_id import SessionInfo, session_id_prefix_of
from modex_agent.core.session_scope_discovery import discover_file_session_pool_map
from modex_agent.core.session_store import LocalFileSessionStore, SessionStore
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    from bot.service.workspace_store import WorkspaceScopedTranscriptStore

SessionStoreResolver = Callable[[Path], Awaitable[SessionStore]]
SessionPoolResolver = Callable[[SessionInfo], str]

logger = logging.getLogger(__name__)


class SessionGcConfig(BaseModel):
    """Global knobs for the session garbage collector (frozen, strict)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    scan_interval_seconds: int = Field(default=300, ge=1)
    max_workers: int = Field(default=1, ge=1)


def load_session_gc_config(raw: dict[str, Any] | None) -> SessionGcConfig:
    """Build SessionGcConfig from the raw top-level config dict.

    The framework ``AppConfig`` ignores business keys (``extra: ignore``), so the
    bot reads ``session_gc`` from the same raw YAML dict itself. Missing or empty
    → all defaults.
    """
    section = (raw or {}).get("session_gc") or {}
    return SessionGcConfig(**section)


class _IndexedSession:
    """A session read from the index, annotated with its owning pool dir."""

    __slots__ = ("info", "pool")

    def __init__(self, info: SessionInfo, pool: str) -> None:
        self.info = info
        self.pool = pool

    @property
    def session_id(self) -> str:
        return self.info.session_id

    @property
    def parent_session_id(self) -> str | None:
        return self.info.parent_session_id


def _read_session_index(paths: WorkspacePaths) -> dict[str, _IndexedSession]:
    """Read every index record under *paths*, keyed by session id.

    Resilient to malformed records (logged + skipped) so a corrupt entry never
    blocks the sweep. Pool is the parent directory of the record file.
    """
    base = paths.session_index_dir
    out: dict[str, _IndexedSession] = {}
    if not base.is_dir():
        return out
    for f in sorted(base.glob("*/*.json")):
        try:
            info = SessionInfo(**json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("session-gc: skipping malformed index record %s", f)
            continue
        out[info.session_id] = _IndexedSession(info, f.parent.name)
    return out


def _find_orphan_sessions(paths: WorkspacePaths) -> list[_IndexedSession]:
    """Rule 1: non-root sessions whose parent index record is gone."""
    graph = _read_session_index(paths)
    return [
        s
        for s in graph.values()
        if s.parent_session_id is not None and s.parent_session_id not in graph
    ]


def _find_children(parent_sid: str, paths: WorkspacePaths) -> list[_IndexedSession]:
    """All index records (across pools) whose parent is *parent_sid*."""
    graph = _read_session_index(paths)
    return [s for s in graph.values() if s.parent_session_id == parent_sid]


def _propagate_children(parent_sid: str, paths: WorkspacePaths) -> list[_IndexedSession]:
    """The children of *parent_sid* that still need cleaning (across all pools).

    Called by the collector after cleaning a session so it can enqueue each
    child as its own ``clean_session`` unit (BFS propagation lives in the pool).
    """
    return _find_children(parent_sid, paths)


def _find_orphan_artifact_sids(paths: WorkspacePaths) -> dict[str, str]:
    """Rule 2: session ids that left artifacts but have no index record.

    Two signals, either sufficient to flag an orphan sid (clean_session then
    recomputes all nine paths wholesale): the memory-session dir
    (``memory/<pool>/session/<sid>/``) and the transcript file
    (``sessions/<pool>/<sid>.jsonl``). Both carry the raw session id (dots
    preserved). ``setdefault`` prefers the memory dir's pool when both exist.
    """
    live = _read_session_index(paths)
    orphans: dict[str, str] = {}
    memory_base = paths.root / "memory"
    if memory_base.is_dir():
        for pool_dir in memory_base.iterdir():
            sess_dir = pool_dir / "session"
            if not sess_dir.is_dir():
                continue
            for child in sess_dir.iterdir():
                if child.is_dir() and child.name not in live:
                    orphans.setdefault(child.name, pool_dir.name)
    sessions_base = paths.sessions_dir
    if sessions_base.is_dir():
        for f in sessions_base.glob("*/*.jsonl"):
            sid = f.stem
            if sid not in live:
                orphans.setdefault(sid, f.parent.name)
    return orphans


def _cleanup_orphan_pool_routes(paths: WorkspacePaths) -> int:
    """Remove ``pool_sessions/<prefix>.json`` entries whose conversation is gone.

    ``pool_sessions`` maps a conversation prefix → the currently-active pool
    (the routing lookup ``PoolRouter`` reads on every incoming message). An
    entry is stale once no live session_index record shares that prefix.

    Done in the sweep (a single file, not a nine-artifact session) and ONLY
    here — never in ``clean_session`` — because a prefix can be shared by more
    than one live session (a conversation switched across pools leaves a root
    in each pool). Per-session deletion could remove a routing entry still
    needed by a sibling session. Safe by construction: the entry is removed only
    once ALL sessions sharing the prefix are gone.
    """
    live_prefixes = {session_id_prefix_of(sid) for sid in _read_session_index(paths)}
    routes_dir = paths.pool_sessions_dir
    if not routes_dir.is_dir():
        return 0
    removed = 0
    for f in routes_dir.glob("*.json"):
        if f.stem not in live_prefixes:
            try:
                f.unlink()
                removed += 1
            except OSError:
                logger.warning("session-gc: could not remove orphan pool route %s", f)
    return removed


class _Job:
    __slots__ = ("scope", "ws_root")

    def __init__(self, scope: BotRecordScope, ws_root: Path) -> None:
        self.scope = scope
        self.ws_root = ws_root


class SessionCleanerOperations(ABC):
    @abstractmethod
    async def discover_orphan_scopes(
        self,
        paths: WorkspacePaths,
        *,
        live_session_ids: frozenset[str],
        workspace_id: str,
    ) -> list[RecordScope]: ...

    @abstractmethod
    async def clean_session_artifacts(
        self,
        paths: WorkspacePaths,
        session_id: str,
        scope: RecordScope,
    ) -> SessionCleanupResult: ...


class _FileSessionCleanerOperations(SessionCleanerOperations):
    async def discover_orphan_scopes(
        self,
        paths: WorkspacePaths,
        *,
        live_session_ids: frozenset[str],
        workspace_id: str,
    ) -> list[RecordScope]:
        cleaner = DefaultSessionArtifactCleaner(paths=paths)
        return await cleaner.discover_orphan_scopes(
            live_session_ids=live_session_ids,
            workspace_id=workspace_id,
        )

    async def clean_session_artifacts(
        self,
        paths: WorkspacePaths,
        session_id: str,
        scope: RecordScope,
    ) -> SessionCleanupResult:
        cleaner = DefaultSessionArtifactCleaner(paths=paths)
        return await cleaner.clean_session_artifacts(session_id, scope)


class SessionGarbageCollector:
    """Crash-safe cascade session garbage collector (ADR-0018).

    Process-level singleton, workspace-independent. Two triggers feed one
    single-worker pool: foreground ``delete_session_tree`` and the periodic
    ``sweep_once``. An in-memory, non-persistent dedup set suppresses concurrent
    duplicates and is cleared in each task's ``finally`` so the backstop is never
    permanently blocked.

    T17: per-session artifact cleanup is delegated to
    :class:`modex_agent.core.cleanup.SessionArtifactCleaner` (default:
    :class:`~modex_agent.core.cleanup.DefaultSessionArtifactCleaner`).
    """

    def __init__(
        self,
        *,
        workspace_roots_provider: Callable[[], Iterable[Path]],
        data_dir_name: str,
        config: SessionGcConfig,
        cleaner_factory: SessionCleanerOperations | None = None,
        transcript_store: WorkspaceScopedTranscriptStore | None = None,
        session_store_resolver: SessionStoreResolver | None = None,
        session_pool_resolver: SessionPoolResolver | None = None,
    ) -> None:
        self._roots_provider = workspace_roots_provider
        self._data_dir_name = data_dir_name
        self._config = config
        self._cleaner_factory = cleaner_factory or _FileSessionCleanerOperations()
        self._transcript_store = transcript_store
        self._session_store_resolver = session_store_resolver
        self._session_pool_resolver = session_pool_resolver
        self._queue: asyncio.Queue[_Job | None] = asyncio.Queue()
        self._inflight: set[tuple[Path, str]] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._sweep_task: asyncio.Task[None] | None = None
        self._stopping = False

    # -- public API ------------------------------------------------------

    async def start(self) -> None:
        for _ in range(self._config.max_workers):
            self._workers.append(asyncio.create_task(self._worker_loop()))
        if self._config.enabled:
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        """Delay-after-completion cadence: interval measured end→start, not fixed."""
        while not self._stopping:
            try:
                await self.sweep_once()
            except Exception:
                logger.exception("session-gc: sweep_once error")
            await asyncio.sleep(self._config.scan_interval_seconds)

    async def stop(self) -> None:
        self._stopping = True
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            await asyncio.gather(self._sweep_task, return_exceptions=True)
            self._sweep_task = None
        for w in self._workers:
            w.cancel()
        # Await so each worker's `finally` (inflight discard) finishes before we
        # clear state — prevents a stale `finally` racing a restart's new inflight.
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        # Drain any leftover sentinels/jobs so a restart starts with an empty queue.
        while not self._queue.empty():
            self._queue.get_nowait()
        self._inflight.clear()

    async def delete_session_tree(
        self,
        root_session_id: str,
        ws_root: Path | None = None,
        pool: str | None = None,
    ) -> None:
        """Foreground trigger: remove the root's record now, enqueue the rest.

        When the caller knows the workspace root and pool (the WebUI delete
        handler resolves both), use them directly so a transcript-only session
        (no index record) is still removed. Otherwise scan every workspace's
        index for the root record. Sync-removing the record + transcript makes
        the conversation leave the list immediately; the cascade drains async.
        """
        if ws_root is not None and pool is not None:
            paths = WorkspacePaths(root=ws_root / self._data_dir_name)
            scope = BotRecordScope(
                session_id=root_session_id,
                pool=pool,
                workspace_id=str(ws_root.resolve()),
            )
            await self._clean_record_and_transcript(
                root_session_id,
                scope.to_path_segment("pool"),
                paths,
            )
            await self._enqueue_persisted_scopes(paths, scope, ws_root)
            return
        if ws_root is not None:
            paths = WorkspacePaths(root=ws_root / self._data_dir_name)
            pool = await self._find_session_pool(paths, root_session_id)
            if pool is not None:
                await self._clean_record_and_transcript(
                    root_session_id,
                    pool,
                    paths,
                )
                await self._enqueue_persisted_scopes(
                    paths,
                    BotRecordScope(
                        session_id=root_session_id,
                        pool=pool,
                        workspace_id=str(ws_root.resolve()),
                    ),
                    ws_root,
                )
                return
        for ws_root in self._roots_provider():
            paths = WorkspacePaths(root=ws_root / self._data_dir_name)
            pool = await self._find_session_pool(paths, root_session_id)
            if pool is None:
                continue
            await self._clean_record_and_transcript(root_session_id, pool, paths)
            await self._enqueue_persisted_scopes(
                paths,
                BotRecordScope(
                    session_id=root_session_id,
                    pool=pool,
                    workspace_id=str(ws_root.resolve()),
                ),
                ws_root,
            )
            return

    async def sweep_once(self) -> None:
        """One backstop pass over all workspaces: enqueue top-layer orphans."""
        if self._config.enabled is False:
            return
        ws_count = 0
        enqueued_sessions = 0
        enqueued_artifacts = 0
        removed_routes = 0
        for ws_root in self._roots_provider():
            if not ws_root.is_dir():
                continue
            ws_count += 1
            paths = WorkspacePaths(root=ws_root / self._data_dir_name)
            sessions = await self._list_sessions(paths)
            graph = {session.session_id: session for session in sessions}
            orphan_sessions = [
                session
                for session in sessions
                if session.parent_session_id is not None and session.parent_session_id not in graph
            ]
            orphan_session_ids = {orphan.session_id for orphan in orphan_sessions}
            workspace_id = str(ws_root.resolve())
            orphan_scopes = await self._cleaner_factory.discover_orphan_scopes(
                paths,
                live_session_ids=frozenset(graph).difference(orphan_session_ids),
                workspace_id=workspace_id,
            )
            pool_map = discover_file_session_pool_map(paths, workspace_id)
            discovered_session_ids = {
                scope.session_id for scope in orphan_scopes if scope.session_id is not None
            }
            for orphan in orphan_sessions:
                if orphan.session_id in discovered_session_ids:
                    continue
                if self._enqueue(
                    BotRecordScope(
                        session_id=orphan.session_id,
                        pool=self._pool_of(paths, orphan),
                        workspace_id=workspace_id,
                    ),
                    ws_root,
                ):
                    enqueued_sessions += 1
            for scope in orphan_scopes:
                if self._enqueue(scope, ws_root, pool_map=pool_map):
                    enqueued_artifacts += 1
            removed_routes += _cleanup_orphan_pool_routes(paths)
        logger.info(
            "session-gc: sweep done across %d workspace(s) — enqueued %d orphan "
            "session(s), %d orphan artifact(s), removed %d pool route(s)",
            ws_count,
            enqueued_sessions,
            enqueued_artifacts,
            removed_routes,
        )

    # -- internals -------------------------------------------------------

    def _enqueue(
        self,
        scope: RecordScope,
        ws_root: Path,
        *,
        pool_map: dict[str, str] | None = None,
    ) -> bool:
        # Framework discovery returns base RecordScope (no pool); wrap so
        # job.scope.pool reads work (ADR-0028 framework/business boundary).
        # When pool_map is provided (from discover_file_session_pool_map),
        # recover the pool directory name for discovered scopes so cleanup
        # targets the correct pool-partitioned directory.
        if isinstance(scope, BotRecordScope):
            bot_scope = scope
        else:
            pool = (
                pool_map.get(scope.canonical(), "default")
                if pool_map is not None
                else "default"
            )
            # scope may already carry a `pool` extra field (e.g. when
            # from_canonical returned a different subclass with the same
            # extra-field signature). Override it with the pool_map result.
            fields = scope.model_dump()
            fields["pool"] = pool
            bot_scope = BotRecordScope(**fields)
        key = (ws_root.resolve(), bot_scope.canonical())
        if key in self._inflight:
            return False
        self._inflight.add(key)
        self._queue.put_nowait(_Job(bot_scope, ws_root))
        return True

    async def _enqueue_persisted_scopes(
        self,
        paths: WorkspacePaths,
        fallback_scope: RecordScope,
        ws_root: Path,
    ) -> None:
        session_id = fallback_scope.session_id
        if session_id is None:
            raise MissingSessionScopeError
        # Use the SQLite-aware _list_sessions instead of the file-only
        # _read_session_index so live session IDs are correct in SQLite mode.
        live_sessions = await self._list_sessions(paths)
        workspace_id = fallback_scope.workspace_id or str(ws_root.resolve())
        discovered = await self._cleaner_factory.discover_orphan_scopes(
            paths,
            live_session_ids=frozenset(s.session_id for s in live_sessions),
            workspace_id=workspace_id,
        )
        pool_map = discover_file_session_pool_map(paths, workspace_id)
        matching = [scope for scope in discovered if scope.session_id == session_id]
        for scope in matching or [fallback_scope]:
            self._enqueue(scope, ws_root, pool_map=pool_map)

    async def _worker_loop(self) -> None:
        while True:
            job = await self._queue.get()
            if job is None:
                return
            await self._process_job(job)

    # -- test helpers (not public API) ----------------------------------
    async def _drain_for_tests(self) -> None:
        """Process queued jobs until the queue is empty (tests only)."""
        while not self._queue.empty():
            job = self._queue.get_nowait()
            if job is None:
                continue
            await self._process_job(job)

    async def _process_job(self, job: _Job) -> None:
        key = (job.ws_root.resolve(), job.scope.canonical())
        try:
            paths = WorkspacePaths(root=job.ws_root / self._data_dir_name)
            session_id = job.scope.session_id
            if session_id is None:
                raise MissingSessionScopeError
            try:
                result = await self._cleaner_factory.clean_session_artifacts(
                    paths,
                    session_id,
                    job.scope,
                )
            except Exception:
                logger.exception(
                    "session-gc: clean_session_artifacts failed for %s (%s); backstop will retry",
                    session_id,
                    job.scope.canonical(),
                )
            else:
                if result.errors:
                    logger.warning(
                        "session-gc: clean_session_artifacts had errors for %s: %s",
                        session_id,
                        result.errors,
                    )
                logger.info(
                    "session-gc: cleaned %s (scope=%s, pool=%s, ws=%s, files=%d, dirs=%d, db_rows=%d)",
                    session_id,
                    job.scope.canonical(),
                    job.scope.pool,
                    job.ws_root,
                    result.files_deleted,
                    result.dirs_deleted,
                    result.db_rows_deleted,
                )
            for child in await self._children(paths, session_id):
                await self._clean_record_and_transcript(
                    child.session_id,
                    self._pool_of(paths, child, fallback=job.scope.pool),
                    paths,
                )
                await self._enqueue_persisted_scopes(
                    paths,
                    BotRecordScope(
                        session_id=child.session_id,
                        pool=self._pool_of(paths, child, fallback=job.scope.pool),
                        workspace_id=job.scope.workspace_id,
                    ),
                    job.ws_root,
                )
        finally:
            self._inflight.discard(key)

    async def _session_store(self, paths: WorkspacePaths) -> SessionStore:
        resolver = self._session_store_resolver
        if resolver is not None:
            return await resolver(paths.session_index_dir)
        return LocalFileSessionStore(paths.session_index_dir)

    async def _find_session_pool(
        self,
        paths: WorkspacePaths,
        session_id: str,
    ) -> str | None:
        """Find the pool for *session_id* using the backend-aware session store.

        Works in both FILE and SQLite modes: delegates to ``_list_sessions``
        which calls the injected ``SessionStore`` (or ``LocalFileSessionStore``
        as fallback). Returns ``None`` if the session is not found.
        """
        for session in await self._list_sessions(paths):
            if session.session_id == session_id:
                return self._pool_of(paths, session)
        return None

    async def _list_sessions(self, paths: WorkspacePaths) -> list[SessionInfo]:
        return await (await self._session_store(paths)).list_sessions()

    async def _children(
        self,
        paths: WorkspacePaths,
        parent_session_id: str,
    ) -> list[SessionInfo]:
        return await (await self._session_store(paths)).get_children(parent_session_id)

    def _pool_of(
        self,
        paths: WorkspacePaths,
        session: SessionInfo,
        *,
        fallback: str | None = None,
    ) -> str:
        if self._session_pool_resolver is not None:
            return self._session_pool_resolver(session)
        pool = session.metadata.get("pool")
        if pool is not None:
            return str(pool)
        # Fall back to the file-based index (pool subdirectory naming).
        # In SQLite mode, the session_pool_resolver should always be wired
        # by the business layer, so this fallback is FILE-mode only.
        indexed = _read_session_index(paths).get(session.session_id)
        if indexed is not None:
            return indexed.pool
        return fallback or session.agent_name

    async def _clean_record_and_transcript(
        self,
        session_id: str,
        pool: str,
        paths: WorkspacePaths,
    ) -> None:
        await asyncio.to_thread(_clean_record_and_transcript, session_id, pool, paths)
        # Synchronously remove the session record from the session store (FILE
        # or SQLite) so the session leaves the list immediately. The file-based
        # cleanup above is a no-op in SQLite mode; this call is what actually
        # removes the row from the ``sessions`` table.
        store = await self._session_store(paths)
        await store.delete(session_id)
        if self._transcript_store is not None:
            await self._transcript_store.delete_session(
                session_id,
                sessions_dir=paths.sessions_dir,
            )

    def _inflight_count(self) -> int:
        return len(self._inflight)


def _remove_unit(unit: Path) -> None:
    try:
        if unit.is_dir():
            shutil.rmtree(unit)
        elif unit.exists():
            unit.unlink()
    except FileNotFoundError:
        pass


def _clean_record_and_transcript(session_id: str, pool: str, paths: WorkspacePaths) -> None:
    units = session_artifact_paths(session_id, pool, paths)
    _remove_unit(next(u for u in units if "session_index" in u.parts))
    _remove_unit(next(u for u in units if u.suffix == ".jsonl"))
