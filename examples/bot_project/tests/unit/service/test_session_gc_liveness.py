"""Tests for SessionGarbageCollector liveness gate + retry mechanism (todo 10).

Six scenarios:
1. Active session -> delete_session_tree returns False, record NOT removed, reservation released.
2. Inactive session -> normal deletion proceeds, reservation acquired + released.
3. _process_job active -> job skipped, _inflight cleared, delayed re-enqueue, attempts incremented.
4. 3 attempts -> job abandoned with warning (no further retry).
5. stop() cancels all delayed re-enqueue tasks.
6. Constructor accepts liveness_provider; backward-compat when None (no gating).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.scope import BotRecordScope
from bot.service.liveness import LivenessProvider
from bot.service.session_gc import (
    SessionCleanerOperations,
    SessionGarbageCollector,
    SessionGcConfig,
    _Job,
)

from modex_agent.core.cleanup import SessionCleanupResult, session_artifact_paths
from modex_agent.core.scope import RecordScope
from modex_agent.workspace.paths import WorkspacePaths

# ── Helpers ──────────────────────────────────────────────────────────────────


def _paths_for(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(root=tmp_path / ".modex")


def _write_index(paths: WorkspacePaths, pool: str, session_id: str, parent: str | None = None) -> None:
    rec = {
        "session_id": session_id,
        "agent_name": session_id.split(".")[-1],
        "parent_session_id": parent,
        "created_at": 0,
        "updated_at": 0,
        "metadata": {},
    }
    d = paths.session_index_dir / pool
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def _seed_full_session(paths: WorkspacePaths, pool: str, sid: str, parent: str | None = None) -> None:
    _write_index(paths, pool, sid, parent)
    for unit in session_artifact_paths(sid, pool, paths):
        if unit.suffix == ".json" and "session_index" in str(unit):
            continue
        if unit.suffix in (".json", ".jsonl"):
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("{}", encoding="utf-8")
        else:
            unit.mkdir(parents=True, exist_ok=True)
            (unit / "data").write_text("x", encoding="utf-8")


class _StubLivenessProvider(LivenessProvider):
    """Controllable liveness provider for tests."""

    def __init__(
        self,
        active_sessions: set[str] | None = None,
        reserve_fail: set[str] | None = None,
    ) -> None:
        self._active = active_sessions or set()
        self._reserve_fail = reserve_fail or set()
        self.reservations_acquired: list[str] = []
        self.reservations_released: list[str] = []

    async def is_session_active(self, session_id: str, workspace_root: Path) -> bool:
        return session_id in self._active

    async def try_reserve_deletion(self, session_id: str, workspace_root: Path) -> bool:
        if session_id in self._reserve_fail:
            return False
        self.reservations_acquired.append(session_id)
        return True

    async def release_deletion(self, session_id: str) -> None:
        self.reservations_released.append(session_id)


class _RecordingFactory(SessionCleanerOperations):
    """Factory that records clean_session_artifacts calls."""

    def __init__(self) -> None:
        self.cleaned: list[tuple[Path, str, RecordScope]] = []

    async def discover_orphan_scopes(
        self,
        paths: WorkspacePaths,
        *,
        live_session_ids: frozenset[str],
        workspace_id: str,
    ) -> list[RecordScope]:
        return []

    async def clean_session_artifacts(
        self,
        paths: WorkspacePaths,
        session_id: str,
        scope: RecordScope,
    ) -> SessionCleanupResult:
        self.cleaned.append((paths.root, session_id, scope))
        return SessionCleanupResult()


def _index_exists(paths: WorkspacePaths, pool: str, sid: str) -> bool:
    return (paths.session_index_dir / pool / f"{sid}.json").exists()


# ── Scenario 1: active -> delete_session_tree returns False ──────────────────


def test_active_session_blocks_delete_and_releases_reservation(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding")
    provider = _StubLivenessProvider(active_sessions={"aaa.coding"})
    factory = _RecordingFactory()
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
        liveness_provider=provider,
    )

    async def _run() -> None:
        result = await gc.delete_session_tree(
            "aaa.coding", ws_root=tmp_path, pool="coding",
        )
        assert result is False
        # Record + transcript NOT removed (liveness gate blocked destruction)
        assert _index_exists(paths, "coding", "aaa.coding")
        # Reservation was acquired then released
        assert "aaa.coding" in provider.reservations_acquired
        assert "aaa.coding" in provider.reservations_released

    asyncio.run(_run())


# ── Scenario 2: inactive -> normal deletion, reservation acquired+released ───


def test_inactive_session_deletes_normally_with_reservation(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding")
    provider = _StubLivenessProvider(active_sessions=set())
    factory = _RecordingFactory()
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
        liveness_provider=provider,
    )

    async def _run() -> None:
        result = await gc.delete_session_tree(
            "aaa.coding", ws_root=tmp_path, pool="coding",
        )
        assert result is True
        # Record removed (deletion proceeded)
        assert not _index_exists(paths, "coding", "aaa.coding")
        # Reservation was acquired then released
        assert "aaa.coding" in provider.reservations_acquired
        assert "aaa.coding" in provider.reservations_released

    asyncio.run(_run())


# ── Scenario 3: _process_job active -> retry scheduled ───────────────────────


def test_process_job_active_schedules_retry(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding")
    provider = _StubLivenessProvider(active_sessions={"aaa.coding"})
    factory = _RecordingFactory()
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
        liveness_provider=provider,
    )
    gc._retry_delay_seconds = 999  # prevent delayed task from firing during test

    scope = BotRecordScope(session_id="aaa.coding", pool="coding")

    async def _run() -> None:
        key = (tmp_path.resolve(), scope.canonical())
        gc._inflight.add(key)
        job = _Job(scope, tmp_path)
        gc._queue.put_nowait(job)
        await gc._drain_for_tests()

        # Cleaner NOT called (destruction skipped)
        assert factory.cleaned == []
        # _inflight cleared by _retry_or_abandon (so re-enqueue won't suppress)
        assert gc._inflight_count() == 0
        # Delayed re-enqueue task scheduled
        assert len(gc._delayed_tasks) == 1
        # Attempts incremented
        assert job.attempts == 1
        # Reservation acquired then released
        assert "aaa.coding" in provider.reservations_acquired
        assert "aaa.coding" in provider.reservations_released

        # Clean up delayed tasks
        for task in list(gc._delayed_tasks):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())


# ── Scenario 4: 3 attempts -> abandoned ──────────────────────────────────────


def test_process_job_abandoned_after_3_attempts(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding")
    provider = _StubLivenessProvider(active_sessions={"aaa.coding"})
    factory = _RecordingFactory()
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
        liveness_provider=provider,
    )
    gc._retry_delay_seconds = 999

    scope = BotRecordScope(session_id="aaa.coding", pool="coding")

    async def _run() -> None:
        # Start with attempts=2 so the next failure (attempt 3) triggers abandonment
        key = (tmp_path.resolve(), scope.canonical())
        gc._inflight.add(key)
        job = _Job(scope, tmp_path, attempts=2)
        gc._queue.put_nowait(job)
        await gc._drain_for_tests()

        # Cleaner NOT called
        assert factory.cleaned == []
        # No delayed task (abandoned, not retried)
        assert len(gc._delayed_tasks) == 0
        # Attempts reached max
        assert job.attempts == 3
        # _inflight cleared
        assert gc._inflight_count() == 0

    asyncio.run(_run())


# ── Scenario 5: stop() cancels delayed tasks ─────────────────────────────────


def test_stop_cancels_delayed_tasks(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding")
    provider = _StubLivenessProvider(active_sessions={"aaa.coding"})
    factory = _RecordingFactory()
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
        liveness_provider=provider,
    )
    gc._retry_delay_seconds = 999

    scope = BotRecordScope(session_id="aaa.coding", pool="coding")

    async def _run() -> None:
        key = (tmp_path.resolve(), scope.canonical())
        gc._inflight.add(key)
        job = _Job(scope, tmp_path)
        gc._queue.put_nowait(job)
        await gc._drain_for_tests()

        # Verify delayed task exists
        assert len(gc._delayed_tasks) == 1
        tasks_before_stop = list(gc._delayed_tasks)

        await gc.stop()

        # All delayed tasks cancelled
        for task in tasks_before_stop:
            assert task.cancelled() or task.done()

    asyncio.run(_run())


# ── Scenario 6: constructor accepts liveness_provider + backward-compat ──────


def test_constructor_accepts_liveness_provider_and_backward_compat(tmp_path: Path) -> None:
    # With provider: stored on the instance
    provider = _StubLivenessProvider()
    gc_with = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        liveness_provider=provider,
    )
    assert gc_with._liveness_provider is provider

    # Without provider: None, gating skipped, delete_session_tree returns True
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "bbb.coding")
    gc_without = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
    )
    assert gc_without._liveness_provider is None

    async def _run() -> None:
        result = await gc_without.delete_session_tree(
            "bbb.coding", ws_root=tmp_path, pool="coding",
        )
        assert result is True
        assert not _index_exists(paths, "coding", "bbb.coding")

    asyncio.run(_run())
