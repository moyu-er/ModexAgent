"""Crash-safe session garbage collection (ADR-0018).

A bot-side collector that deletes a conversation's full cascade (root + every
subagent descendant via ``parent_session_id``) and all ten per-session artifact
types. Crash recoverability is the first constraint: deletion progress is fully
reconstructable from disk (the session-index graph) after a restart, with no
in-memory closure collected up front. See ADR-0018 and the session-lifecycle
glossary in CONTEXT.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.session_id import SessionInfo, agent_of, session_id_prefix_of
from modex_agent.core.session_store import safe_filename
from modex_agent.memory.stores.utils import sanitize_scope_key
from modex_agent.runtime.store import JsonFileTodoStore, JsonFileTurnStateStore
from modex_agent.workspace.paths import WorkspacePaths, safe_segment

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


_UPLOADS_SUBDIR = "uploads"


def _session_artifact_paths(session_id: str, pool: str, paths: WorkspacePaths) -> list[Path]:
    """The ten per-session artifact units for *session_id* under *pool*.

    Each entry is a whole per-session directory or file (never a sub-file inside
    a dir), derived with the same on-disk transform its store uses. Caller may
    delete any that exist; all are tolerant of being already absent.
    """
    agent = agent_of(session_id)
    prefix = session_id_prefix_of(session_id)
    safe = safe_filename(session_id)
    scope = sanitize_scope_key(session_id)
    seg = safe_segment(session_id)

    return [
        paths.sessions_dir / pool / f"{safe}.jsonl",                      # transcript
        paths.session_index_dir / pool / f"{safe}.json",                  # index record
        paths.memory_dir(pool) / "session" / scope,                       # memory messages
        paths.pruned_dir(pool) / scope,                                   # pruned batches
        paths.fork_contexts_dir(pool) / f"{agent}_{prefix}.xml",          # fork context
        paths.media_dir(pool) / _UPLOADS_SUBDIR / seg,                    # media uploads
        paths.runtime_dir(pool, "trace") / session_id,                    # trace (raw)
        paths.runtime_dir(pool, "output") / session_id,                   # output (raw)
        paths.runtime_dir(pool, "todos") / f"{JsonFileTodoStore._safe_segment(session_id)}.json",
        paths.runtime_dir(pool, "turns")
        / JsonFileTurnStateStore._safe_segment(agent)
        / JsonFileTurnStateStore._safe_segment(session_id),               # turn state
    ]


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
        s for s in graph.values()
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
    recomputes all ten paths wholesale): the memory-session dir
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


async def clean_session(session_id: str, pool: str, paths: WorkspacePaths) -> None:
    """Idempotently remove one session's record + transcript + ten artifacts.

    Order is index-first (Path B): the existence marker goes before the
    artifacts, so a mid-cleanup session vanishes from the orphan-session rule's
    view almost immediately. Any OSError aborts this session for this round —
    the backstop sweep re-discovers and retries it. Missing targets are no-ops.
    """
    await asyncio.to_thread(_clean_session_sync, session_id, pool, paths)


def _clean_session_sync(session_id: str, pool: str, paths: WorkspacePaths) -> None:
    units = _session_artifact_paths(session_id, pool, paths)
    # index record first (existence marker), then transcript, then the rest
    index_unit = next(u for u in units if "session_index" in u.parts)
    _remove_unit(index_unit)
    transcript_unit = next(u for u in units if u.suffix == ".jsonl")
    _remove_unit(transcript_unit)
    for unit in units:
        if unit in (index_unit, transcript_unit):
            continue
        _remove_unit(unit)


def _remove_unit(unit: Path) -> None:
    # ignore_errors=False: a locked/active file raises and aborts this session
    # this round (backstop retries). FileNotFoundError is suppressed.
    try:
        if unit.is_dir():
            shutil.rmtree(unit)
        elif unit.exists():
            unit.unlink()
    except FileNotFoundError:
        pass


def _cleanup_orphan_pool_routes(paths: WorkspacePaths) -> int:
    """Remove ``pool_sessions/<prefix>.json`` entries whose conversation is gone.

    ``pool_sessions`` maps a conversation prefix → the currently-active pool
    (the routing lookup ``PoolRouter`` reads on every incoming message). An
    entry is stale once no live session_index record shares that prefix.

    Done in the sweep (a single file, not a ten-artifact session) and ONLY
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
    __slots__ = ("session_id", "pool", "ws_root")

    def __init__(self, session_id: str, pool: str, ws_root: Path) -> None:
        self.session_id = session_id
        self.pool = pool
        self.ws_root = ws_root


class SessionGarbageCollector:
    """Crash-safe cascade session garbage collector (ADR-0018).

    Process-level singleton, workspace-independent. Two triggers feed one
    single-worker pool: foreground ``delete_session_tree`` and the periodic
    ``sweep_once``. An in-memory, non-persistent dedup set suppresses concurrent
    duplicates and is cleared in each task's ``finally`` so the backstop is never
    permanently blocked.
    """

    def __init__(
        self,
        *,
        workspace_roots_provider: Callable[[], Iterable[Path]],
        data_dir_name: str,
        config: SessionGcConfig,
    ) -> None:
        self._roots_provider = workspace_roots_provider
        self._data_dir_name = data_dir_name
        self._config = config
        self._queue: asyncio.Queue[_Job | None] = asyncio.Queue()
        self._inflight: set[str] = set()
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
            await asyncio.to_thread(_clean_record_and_transcript, root_session_id, pool, paths)
            self._enqueue(root_session_id, pool, ws_root)
            return
        for ws_root in self._roots_provider():
            paths = WorkspacePaths(root=ws_root / self._data_dir_name)
            graph = _read_session_index(paths)
            node = graph.get(root_session_id)
            if node is None:
                continue
            await asyncio.to_thread(_clean_record_and_transcript, root_session_id, node.pool, paths)
            self._enqueue(root_session_id, node.pool, ws_root)
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
            for orphan in _find_orphan_sessions(paths):
                if self._enqueue(orphan.session_id, orphan.pool, ws_root):
                    enqueued_sessions += 1
            for sid, pool in _find_orphan_artifact_sids(paths).items():
                if self._enqueue(sid, pool, ws_root):
                    enqueued_artifacts += 1
            removed_routes += _cleanup_orphan_pool_routes(paths)
        logger.info(
            "session-gc: sweep done across %d workspace(s) — enqueued %d orphan "
            "session(s), %d orphan artifact(s), removed %d pool route(s)",
            ws_count, enqueued_sessions, enqueued_artifacts, removed_routes,
        )

    # -- internals -------------------------------------------------------

    def _enqueue(self, session_id: str, pool: str, ws_root: Path) -> bool:
        if session_id in self._inflight:
            return False
        self._inflight.add(session_id)
        self._queue.put_nowait(_Job(session_id, pool, ws_root))
        return True

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
        try:
            paths = WorkspacePaths(root=job.ws_root / self._data_dir_name)
            try:
                await clean_session(job.session_id, job.pool, paths)
            except Exception:
                # Any failure (OSError on a locked file, path error, ...) aborts
                # this session for this round. The backstop sweep re-discovers
                # and retries it. Never let an exception kill the sole worker.
                logger.exception(
                    "session-gc: clean_session failed for %s; backstop will retry",
                    job.session_id,
                )
            else:
                logger.info(
                    "session-gc: cleaned %s (pool=%s, ws=%s)",
                    job.session_id, job.pool, job.ws_root,
                )
            # BFS propagation: enqueue children (self-propagating unit)
            for child in _propagate_children(job.session_id, paths):
                self._enqueue(child.session_id, child.pool, job.ws_root)
        finally:
            self._inflight.discard(job.session_id)

    def _inflight_count(self) -> int:
        return len(self._inflight)


def _clean_record_and_transcript(session_id: str, pool: str, paths: WorkspacePaths) -> None:
    units = _session_artifact_paths(session_id, pool, paths)
    _remove_unit(next(u for u in units if "session_index" in u.parts))
    _remove_unit(next(u for u in units if u.suffix == ".jsonl"))
