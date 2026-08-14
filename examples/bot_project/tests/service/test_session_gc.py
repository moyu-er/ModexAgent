# tests/service/test_session_gc.py
from bot.service.session_gc import (
    SessionCleanerOperations,
    SessionGcConfig,
    load_session_gc_config,
)


def test_config_defaults_when_key_absent() -> None:
    cfg = load_session_gc_config({})
    assert cfg.enabled is True
    assert cfg.scan_interval_seconds == 300
    assert cfg.max_workers == 1


def test_config_overrides_from_raw_dict() -> None:
    cfg = load_session_gc_config({"session_gc": {"enabled": False, "scan_interval_seconds": 60, "max_workers": 2}})
    assert cfg.enabled is False
    assert cfg.scan_interval_seconds == 60
    assert cfg.max_workers == 2


def test_config_is_frozen_and_strict() -> None:
    import pydantic
    try:
        SessionGcConfig(scan_interval_seconds=-1)  # type: ignore[arg-type]
    except pydantic.ValidationError:
        pass
    # frozen: assignment must raise
    cfg = SessionGcConfig()
    try:
        cfg.enabled = False  # type: ignore[misc]
        raise AssertionError("expected frozen error")
    except Exception:
        pass


from pathlib import Path

from bot.service.session_gc import _find_children, _find_orphan_sessions, _read_session_index

from modex_agent.core.cleanup import session_artifact_paths as _session_artifact_paths


def _paths_for(tmp_path: Path):
    from modex_agent.workspace.paths import WorkspacePaths
    return WorkspacePaths(root=tmp_path / ".modex")


def test_artifact_paths_all_nine_with_correct_naming(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    sid = "009fc886ecba.coding"
    pool = "coding"
    ap = _session_artifact_paths(sid, pool, paths)

    # transcript + index: safe_filename (dot kept), pool-partitioned
    assert (paths.sessions_dir / pool / "009fc886ecba.coding.jsonl") in ap
    assert (paths.session_index_dir / pool / "009fc886ecba.coding.json") in ap
    # memory session + pruned: sanitize_scope_key (dot kept)
    assert (paths.memory_dir(pool) / "session" / "009fc886ecba.coding") in ap
    assert (paths.pruned_dir(pool) / "009fc886ecba.coding") in ap
    # media uploads: safe_segment (dot -> _)
    assert (paths.media_dir(pool) / "uploads" / "009fc886ecba_coding") in ap
    # runtime trace + output: raw sid
    assert (paths.runtime_dir(pool, "trace") / "009fc886ecba.coding") in ap
    assert (paths.runtime_dir(pool, "output") / "009fc886ecba.coding") in ap
    # todos: dot-preserving -> sid.json
    assert (paths.runtime_dir(pool, "todos") / "009fc886ecba.coding.json") in ap
    # turns: hash-suffix segment under agent/session dirs
    from modex_agent.runtime.store import JsonFileTurnStateStore
    seg_agent = JsonFileTurnStateStore._safe_segment("coding")
    seg_sid = JsonFileTurnStateStore._safe_segment(sid)
    assert (paths.runtime_dir(pool, "turns") / seg_agent / seg_sid) in ap
    # exactly nine units (fork_contexts removed in T17)
    assert len(ap) == 9
    # fork_contexts must NOT appear
    assert not any("fork_contexts" in str(p) for p in ap)


def test_artifact_paths_excludes_pool_shared(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    ap = _session_artifact_paths("x.main", "main", paths)
    # archive + core are pool-shared, must NOT appear
    assert not any("archive" in str(p) for p in ap)
    assert not any("core" in str(p) for p in ap)
    # commands leaf is unused, must NOT appear
    assert not any("commands" in str(p) for p in ap)


import json


def _write_index(paths, pool, session_id, parent=None) -> None:
    rec = {"session_id": session_id, "agent_name": session_id.split(".")[-1],
           "parent_session_id": parent, "created_at": 0, "updated_at": 0, "metadata": {}}
    d = paths.session_index_dir / pool
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_read_index_builds_parent_graph(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    _write_index(paths, "coding", "aaa.coding", None)
    _write_index(paths, "coding", "bbb.worker", "aaa.coding")
    graph = _read_session_index(paths)
    assert graph["aaa.coding"].parent_session_id is None
    assert graph["bbb.worker"].parent_session_id == "aaa.coding"


def test_orphan_sessions_when_parent_gone(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    # root index removed (simulates interrupted cascade); child remains
    _write_index(paths, "coding", "bbb.worker", "aaa.coding")
    _write_index(paths, "coding", "ccc.scout", "bbb.worker")
    orphans = _find_orphan_sessions(paths)
    orphan_ids = {o.session_id for o in orphans}
    # bbb is orphan (parent aaa gone); ccc is NOT yet (parent bbb still present)
    assert "bbb.worker" in orphan_ids
    assert "ccc.scout" not in orphan_ids


def test_find_children_across_pools(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    _write_index(paths, "coding", "aaa.coding", None)
    _write_index(paths, "research", "ddd.researcher", "aaa.coding")
    children = _find_children("aaa.coding", paths)
    child_ids = {(c.session_id, c.pool) for c in children}
    assert ("ddd.researcher", "research") in child_ids


from bot.service.session_gc import _find_orphan_artifact_sids


def test_orphan_artifacts_when_index_gone(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    # a memory session dir exists for a sid with NO index record
    mem = paths.memory_dir("coding") / "session" / "orphan.coding"
    mem.mkdir(parents=True)
    (mem / "messages.jsonl").write_text("{}", encoding="utf-8")
    # a live session: has both index and memory dir -> not orphan
    _write_index(paths, "coding", "live.coding", None)
    (paths.memory_dir("coding") / "session" / "live.coding").mkdir(parents=True)

    found = _find_orphan_artifact_sids(paths)
    assert "orphan.coding" in found
    assert "live.coding" not in found


import asyncio

from bot.scope import BotRecordScope

from modex_agent.core.cleanup import (
    DefaultSessionArtifactCleaner,
    SessionArtifactCleaner,
    SessionCleanupResult,
)
from modex_agent.core.scope import RecordScope
from modex_agent.workspace.paths import WorkspacePaths


class _RecordingFactory(SessionCleanerOperations):
    def __init__(
        self,
        discovered_by_root: dict[Path, list[RecordScope]] | None = None,
    ) -> None:
        self.discovered_by_root = discovered_by_root or {}
        self.discovery_calls: list[tuple[Path, frozenset[str], str]] = []
        self.cleaned: list[tuple[Path, str, RecordScope]] = []

    async def discover_orphan_scopes(
        self,
        paths: WorkspacePaths,
        *,
        live_session_ids: frozenset[str],
        workspace_id: str,
    ) -> list[RecordScope]:
        self.discovery_calls.append((paths.root, live_session_ids, workspace_id))
        return self.discovered_by_root.get(paths.root, [])

    async def clean_session_artifacts(
        self,
        paths: WorkspacePaths,
        session_id: str,
        scope: RecordScope,
    ) -> SessionCleanupResult:
        self.cleaned.append((paths.root, session_id, scope))
        return SessionCleanupResult()


def _seed_full_session(paths, pool, sid, parent=None) -> None:
    _write_index(paths, pool, sid, parent)
    for unit in _session_artifact_paths(sid, pool, paths):
        if unit.suffix == ".json" and "session_index" in str(unit):
            continue  # already written by _write_index above
        if unit.suffix in (".json", ".jsonl"):
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("{}", encoding="utf-8")
        else:
            unit.mkdir(parents=True, exist_ok=True)
            (unit / "data").write_text("x", encoding="utf-8")


def _cleaner(paths):
    return DefaultSessionArtifactCleaner(paths=paths)


def test_collector_retries_when_cleaner_operation_fails(tmp_path) -> None:
    class _FailingFactory(_RecordingFactory):
        async def clean_session_artifacts(
            self,
            paths: WorkspacePaths,
            session_id: str,
            scope: RecordScope,
        ) -> SessionCleanupResult:
            raise LookupError

    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=_FailingFactory(),
    )
    gc._enqueue(BotRecordScope(session_id="aaa.coding", pool="coding"), tmp_path)

    asyncio.run(gc._drain_for_tests())

    assert gc._inflight_count() == 0


def test_collector_passes_typed_pool_session_scope_to_cleaner(
    tmp_path: Path,
) -> None:
    cleaner = _RecordingFactory()
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=cleaner,
    )
    scope = BotRecordScope(session_id="aaa.coding", pool="coding")
    gc._enqueue(scope, tmp_path)

    asyncio.run(gc._drain_for_tests())

    assert cleaner.cleaned == [(tmp_path / ".modex", "aaa.coding", scope)]


def test_collector_forwards_full_workspace_scope_unchanged(tmp_path: Path) -> None:
    scope = BotRecordScope(
        pool="coding",
        workspace_id="workspace-1",
        session_id="aaa.coding",
        agent_id="coding",
        user_id="user-7",
        channel="webui",
        chat_id="chat-9",
    )
    factory = _RecordingFactory()
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
    )

    assert gc._enqueue(scope, tmp_path) is True
    asyncio.run(gc._drain_for_tests())

    assert factory.cleaned == [
        (tmp_path / ".modex", "aaa.coding", scope),
    ]


def test_sweep_discovers_and_cleans_db_only_orphan_scope(tmp_path: Path) -> None:
    scope = BotRecordScope(
        pool="coding",
        workspace_id=str(tmp_path.resolve()),
        session_id="db-only.coding",
        agent_id="coding",
        tenant_id="tenant-1",
    )
    factory = _RecordingFactory({tmp_path / ".modex": [scope]})
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
    )

    async def _run() -> None:
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())

    assert factory.cleaned == [(tmp_path / ".modex", "db-only.coding", scope)]


def test_sweep_recovers_db_only_orphan_from_existing_database(tmp_path: Path) -> None:
    from bot.service.session_cleaner_factory import SessionCleanerFactory

    from modex_agent.persistence.config import PersistenceBackend
    from modex_agent.persistence.managers import WorkspacePersistenceManager

    paths = _paths_for(tmp_path)
    scope = BotRecordScope(
        pool="coding",
        workspace_id=str(tmp_path.resolve()),
        session_id="db-only.coding",
        agent_id="coding",
        user_id="user-7",
    )

    async def _run() -> int:
        manager = WorkspacePersistenceManager(paths.state_db)
        await manager.open()
        await manager.connection.execute(
            "INSERT INTO sessions (session_id, scope_key) VALUES (?, ?)",
            (scope.session_id, scope.canonical()),
        )
        await manager.close()
        gc = SessionGarbageCollector(
            workspace_roots_provider=lambda: [tmp_path],
            data_dir_name=".modex",
            config=SessionGcConfig(max_workers=1),
            cleaner_factory=SessionCleanerFactory(
                backend=PersistenceBackend.SQLITE,
                persistence_resolver=lambda _root: None,
            ),
        )
        await gc.sweep_once()
        await gc._drain_for_tests()
        verification_manager = WorkspacePersistenceManager(paths.state_db)
        await verification_manager.open()
        remaining = await verification_manager.connection.query_value(
            "SELECT COUNT(*) FROM sessions",
            int,
        )
        await verification_manager.close()
        return remaining

    assert asyncio.run(_run()) == 0


def test_dedup_keeps_same_session_with_distinct_scope_identities(tmp_path: Path) -> None:
    first = BotRecordScope(
        workspace_id=str(tmp_path.resolve()),
        session_id="shared.main",
        pool="main",
        user_id="first",
    )
    second = first.model_copy(update={"user_id": "second"})
    factory = _RecordingFactory()
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
    )

    assert gc._enqueue(first, tmp_path) is True
    assert gc._enqueue(second, tmp_path) is True
    asyncio.run(gc._drain_for_tests())

    assert [entry[2] for entry in factory.cleaned] == [first, second]


def test_dedup_keeps_same_scope_in_distinct_workspace_roots(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    scope = BotRecordScope(session_id="shared.main", pool="main")
    factory = _RecordingFactory()
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [first_root, second_root],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
    )

    assert gc._enqueue(scope, first_root) is True
    assert gc._enqueue(scope, second_root) is True
    asyncio.run(gc._drain_for_tests())

    assert [entry[0] for entry in factory.cleaned] == [
        first_root / ".modex",
        second_root / ".modex",
    ]


def test_sweep_passes_live_session_ids_to_discovery(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    _write_index(paths, "main", "live.main", None)
    factory = _RecordingFactory()
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
    )

    asyncio.run(gc.sweep_once())

    assert factory.discovery_calls == [
        (paths.root, frozenset({"live.main"}), str(tmp_path.resolve())),
    ]


def test_foreground_delete_uses_discovered_exact_scope(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "main", "root.main")
    exact_scope = BotRecordScope(
        pool="main",
        workspace_id=str(tmp_path.resolve()),
        session_id="root.main",
        agent_id="main",
        user_id="user-1",
    )
    factory = _RecordingFactory({paths.root: [exact_scope]})
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
    )

    async def _run() -> None:
        await gc.delete_session_tree("root.main", ws_root=tmp_path, pool="main")
        await gc._drain_for_tests()

    asyncio.run(_run())

    assert factory.cleaned == [(paths.root, "root.main", exact_scope)]


def test_foreground_delete_resolves_pool_when_workspace_is_known(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "root.main")
    exact_scope = BotRecordScope(
        pool="coding",
        workspace_id=str(tmp_path.resolve()),
        session_id="root.main",
        agent_id="main",
        user_id="user-1",
    )
    factory = _RecordingFactory({paths.root: [exact_scope]})
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
    )

    async def _run() -> None:
        await gc.delete_session_tree("root.main", ws_root=tmp_path)
        await gc._drain_for_tests()

    asyncio.run(_run())

    assert factory.cleaned == [(paths.root, "root.main", exact_scope)]
    assert "root.main" not in _read_session_index(paths)


def test_child_propagation_uses_discovered_exact_scope(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "main", "root.main")
    _seed_full_session(paths, "coding", "child.worker", "root.main")
    child_scope = BotRecordScope(
        pool="coding",
        workspace_id=str(tmp_path.resolve()),
        session_id="child.worker",
        agent_id="worker",
        invocation_id="invocation-1",
        parent_session_id="root.main",
    )
    factory = _RecordingFactory({paths.root: [child_scope]})
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
    )

    async def _run() -> None:
        await gc.delete_session_tree("root.main", ws_root=tmp_path, pool="main")
        await gc._drain_for_tests()

    asyncio.run(_run())

    assert child_scope in [entry[2] for entry in factory.cleaned]


def test_sweep_parent_orphan_uses_discovered_exact_scope(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "child.worker", "missing.main")
    exact_scope = BotRecordScope(
        pool="coding",
        workspace_id=str(tmp_path.resolve()),
        session_id="child.worker",
        agent_id="worker",
        user_id="user-1",
        parent_session_id="missing.main",
    )
    factory = _RecordingFactory({paths.root: [exact_scope]})
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=factory,
    )

    async def _run() -> None:
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())

    assert factory.cleaned == [(paths.root, "child.worker", exact_scope)]


def test_clean_session_removes_all_nine_units(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding")
    cleaner = _cleaner(paths)
    scope = BotRecordScope(session_id="aaa.coding", pool="coding")
    asyncio.run(cleaner.clean_session_artifacts("aaa.coding", scope))
    for unit in _session_artifact_paths("aaa.coding", "coding", paths):
        assert not unit.exists(), f"still present: {unit}"


def test_clean_session_idempotent_when_already_gone(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    cleaner = _cleaner(paths)
    scope = BotRecordScope(session_id="ghost.coding", pool="coding")
    asyncio.run(cleaner.clean_session_artifacts("ghost.coding", scope))  # must not raise
    asyncio.run(cleaner.clean_session_artifacts("ghost.coding", scope))  # twice is fine


def test_clean_session_preserves_pool_shared_and_siblings(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding")
    _seed_full_session(paths, "coding", "sib.coding")
    archive = paths.memory_dir("coding") / "archive"
    archive.mkdir(parents=True)
    (archive / "state.json").write_text("{}", encoding="utf-8")
    core_mem = paths.memory_dir("coding") / "core"
    core_mem.mkdir(parents=True)
    (core_mem / "MEMORY.md").write_text("kept", encoding="utf-8")

    cleaner = _cleaner(paths)
    scope = BotRecordScope(session_id="aaa.coding", pool="coding")
    asyncio.run(cleaner.clean_session_artifacts("aaa.coding", scope))

    # sibling untouched
    assert (paths.session_index_dir / "coding" / "sib.coding.json").exists()
    # pool-shared untouched
    assert (archive / "state.json").exists()
    assert (core_mem / "MEMORY.md").read_text(encoding="utf-8") == "kept"


from bot.service.session_gc import _propagate_children


def test_propagate_children_returns_descendants_across_pools(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    _seed_full_session(paths, "coding", "bbb.worker", "aaa.coding")
    _seed_full_session(paths, "research", "ccc.researcher", "bbb.worker")  # nested + cross-pool
    kids = _propagate_children("aaa.coding", paths)
    ids = {k.session_id for k in kids}
    assert ids == {"bbb.worker"}
    grandkids = _propagate_children("bbb.worker", paths)
    assert {k.session_id for k in grandkids} == {"ccc.researcher"}


from typing import Never

from bot.service.session_gc import SessionGarbageCollector


def _collector(tmp_path):
    return SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
    )


def test_delete_session_tree_drains_full_cascade(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    _seed_full_session(paths, "coding", "bbb.worker", "aaa.coding")
    _seed_full_session(paths, "coding", "ccc.scout", "bbb.worker")  # nested
    gc = _collector(tmp_path)

    async def _run() -> None:
        await gc.delete_session_tree("aaa.coding")
        await gc._drain_for_tests()

    asyncio.run(_run())
    for sid in ("aaa.coding", "bbb.worker", "ccc.scout"):
        for unit in _session_artifact_paths(sid, "coding", paths):
            assert not unit.exists(), f"{sid} still has {unit}"


def test_sweep_once_completes_interrupted_cascade(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    # simulate: root already gone, child remains (interrupted cascade)
    _seed_full_session(paths, "coding", "bbb.worker", "aaa.coding")
    gc = _collector(tmp_path)

    async def _run() -> None:
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    for unit in _session_artifact_paths("bbb.worker", "coding", paths):
        assert not unit.exists()


def test_sweep_once_removes_orphan_artifacts(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "orphan.coding", None)
    # now delete ONLY its index, leaving artifacts (simulated crash after index)
    (paths.session_index_dir / "coding" / "orphan.coding.json").unlink()
    gc = _collector(tmp_path)

    async def _run() -> None:
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    assert not (paths.memory_dir("coding") / "session" / "orphan.coding").exists()


def test_dedup_suppresses_concurrent_duplicate(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    gc = _collector(tmp_path)
    scope = BotRecordScope(session_id="aaa.coding", pool="coding")
    added1 = gc._enqueue(scope, tmp_path)
    added2 = gc._enqueue(scope, tmp_path)
    assert added1 is True
    assert added2 is False
    assert gc._inflight_count() == 1


def test_start_stop_clean(tmp_path) -> None:
    gc = _collector(tmp_path)

    async def _run() -> None:
        await gc.start()
        assert gc._sweep_task is not None

    asyncio.run(_run())
    # stop outside the start-loop to also exercise the no-running-loop path
    async def _stop() -> None:
        await gc.stop()

    asyncio.run(_stop())
    assert gc._sweep_task is None
    assert gc._inflight_count() == 0
    # restart-safe: a second start works on a fresh state
    async def _run2() -> None:
        await gc.start()
        await gc.stop()

    asyncio.run(_run2())
    assert gc._sweep_task is None


def test_sweep_covers_multiple_workspaces(tmp_path) -> None:
    home = tmp_path / "home"
    other = tmp_path / "other"
    home.mkdir()
    other.mkdir()
    other_paths = _paths_for(other)
    # orphan in OTHER workspace only (parent "gone.coding" has no index)
    _seed_full_session(other_paths, "coding", "zzz.coding", "gone.coding")
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [home, other],
        data_dir_name=".modex",
        config=SessionGcConfig(),
    )

    async def _run() -> None:
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    for unit in _session_artifact_paths("zzz.coding", "coding", other_paths):
        assert not unit.exists()


def test_sweep_catches_orphan_transcript(tmp_path) -> None:
    paths = _paths_for(tmp_path)
    # a transcript with NO index record and NO memory dir (orphan transcript)
    tf = paths.sessions_dir / "coding" / "ghost.coding.jsonl"
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_text("{}\n", encoding="utf-8")
    gc = _collector(tmp_path)

    async def _run() -> None:
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    assert not tf.exists()


def test_dedup_removed_on_clean_failure(tmp_path) -> None:
    """A sid is removed from inflight even when the cleaner raises (backstop can retry)."""
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)

    class _BoomCleaner(SessionArtifactCleaner):
        async def clean_session_artifacts(self, session_id, scope) -> Never:
            raise OSError("simulated locked file")

        async def discover_orphan_scopes(
            self,
            *,
            live_session_ids: frozenset[str],
            workspace_id: str,
        ) -> list[RecordScope]:
            return []

    class _BoomFactory(_RecordingFactory):
        async def clean_session_artifacts(
            self,
            paths: WorkspacePaths,
            session_id: str,
            scope: RecordScope,
        ) -> SessionCleanupResult:
            return await _BoomCleaner().clean_session_artifacts(session_id, scope)

    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
        cleaner_factory=_BoomFactory(),
    )
    gc._enqueue(BotRecordScope(session_id="aaa.coding", pool="coding"), tmp_path)

    async def _run() -> None:
        await gc._drain_for_tests()

    asyncio.run(_run())
    assert gc._inflight_count() == 0


def test_delete_session_tree_idempotent_when_repeated(tmp_path) -> None:
    """Calling delete_session_tree twice is safe; the duplicate enqueue is deduped."""
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    gc = _collector(tmp_path)

    async def _run() -> None:
        await gc.delete_session_tree("aaa.coding", ws_root=tmp_path, pool="coding")
        await gc.delete_session_tree("aaa.coding", ws_root=tmp_path, pool="coding")
        await gc._drain_for_tests()

    asyncio.run(_run())
    assert not (paths.session_index_dir / "coding" / "aaa.coding.json").exists()


def test_sweep_drains_multi_layer_orphan_tree_in_one_pass(tmp_path) -> None:
    """Top-layer sweep + in-pool BFS propagation drains a multi-layer orphan tree
    in a single sweep_once + drain (ccc is not an orphan at sweep time — its parent
    bbb is still present — but is reached via bbb's propagation once bbb is cleaned)."""
    paths = _paths_for(tmp_path)
    # root aaa gone; child bbb + grandchild ccc remain
    _seed_full_session(paths, "coding", "bbb.worker", "aaa.coding")
    _seed_full_session(paths, "coding", "ccc.scout", "bbb.worker")
    gc = _collector(tmp_path)

    async def _run() -> None:
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    for sid in ("bbb.worker", "ccc.scout"):
        for unit in _session_artifact_paths(sid, "coding", paths):
            assert not unit.exists(), f"{sid} still has {unit}"


def test_cleanup_orphan_pool_routes(tmp_path) -> None:
    """Orphan pool_sessions entries (no live session shares the prefix) are removed;
    live and shared-prefix entries are kept."""
    from bot.service.session_gc import _cleanup_orphan_pool_routes

    paths = _paths_for(tmp_path)
    routes = paths.pool_sessions_dir
    routes.mkdir(parents=True, exist_ok=True)
    # live: session exists with prefix 'live1' -> route kept
    _write_index(paths, "coding", "live1.coding", None)
    (routes / "live1.json").write_text(
        json.dumps({"pool": "coding", "session_id": "live1"}), encoding="utf-8"
    )
    # orphan: no session with prefix 'dead1' -> removed
    (routes / "dead1.json").write_text(
        json.dumps({"pool": "main", "session_id": "dead1"}), encoding="utf-8"
    )
    # shared prefix: two live roots (coding + main) -> route kept until both gone
    _write_index(paths, "coding", "shared1.coding", None)
    _write_index(paths, "main", "shared1.main", None)
    (routes / "shared1.json").write_text(
        json.dumps({"pool": "coding", "session_id": "shared1"}), encoding="utf-8"
    )

    removed = _cleanup_orphan_pool_routes(paths)
    assert removed == 1
    assert (routes / "live1.json").exists()
    assert not (routes / "dead1.json").exists()
    assert (routes / "shared1.json").exists()


def test_sweep_once_cleans_orphan_pool_routes(tmp_path) -> None:
    """sweep_once removes orphan pool_sessions routing entries (end-to-end)."""
    paths = _paths_for(tmp_path)
    routes = paths.pool_sessions_dir
    routes.mkdir(parents=True, exist_ok=True)
    (routes / "gone.json").write_text(
        json.dumps({"pool": "main", "session_id": "gone"}), encoding="utf-8"
    )
    gc = _collector(tmp_path)

    async def _run() -> None:
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    assert not (routes / "gone.json").exists()


def test_clean_session_emits_log(tmp_path, caplog) -> None:
    """Cleaning a session emits one log line with its id/pool/workspace."""
    import logging

    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    gc = _collector(tmp_path)
    gc._enqueue(BotRecordScope(session_id="aaa.coding", pool="coding"), tmp_path)

    with caplog.at_level(logging.INFO, logger="bot.service.session_gc"):
        async def _run() -> None:
            await gc._drain_for_tests()

        asyncio.run(_run())
    assert any(
        "aaa.coding" in r.message and "cleaned" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]


def test_sweep_once_emits_summary(tmp_path, caplog) -> None:
    """sweep_once emits a summary line with workspace/orphan/route counts."""
    import logging

    paths = _paths_for(tmp_path)
    routes = paths.pool_sessions_dir
    routes.mkdir(parents=True, exist_ok=True)
    (routes / "gone.json").write_text(
        json.dumps({"pool": "main", "session_id": "gone"}), encoding="utf-8"
    )
    gc = _collector(tmp_path)

    with caplog.at_level(logging.INFO, logger="bot.service.session_gc"):
        async def _run() -> None:
            await gc.sweep_once()

        asyncio.run(_run())
    summary = [r.message for r in caplog.records if "sweep done" in r.message]
    assert summary, [r.message for r in caplog.records]
    assert "removed 1 pool route" in summary[0]


# ── SQLite-specific regression tests ──────────────────────────────────────────


def test_sqlite_delete_session_tree_removes_sessions_row_synchronously(tmp_path) -> None:
    """delete_session_tree must synchronously remove the ``sessions`` table row.

    Regression: before the fix, ``_clean_record_and_transcript`` only removed
    file-based artifacts (no-op in SQLite mode) and transcript events. The
    ``sessions`` table row was only cleaned by the background worker via
    ``delete_session_rows``, so ``GET /api/sessions`` still listed the deleted
    conversation until the async job ran.
    """
    from bot.service.session_cleaner_factory import SessionCleanerFactory

    from modex_agent.core.session_id import SessionInfo
    from modex_agent.persistence.adapters.session_store import SqliteSessionStore
    from modex_agent.persistence.config import PersistenceBackend
    from modex_agent.persistence.managers import WorkspacePersistenceManager

    paths = _paths_for(tmp_path)
    session_id = "f827db2b9945.default"
    pool = "default"

    async def _run() -> int:
        manager = WorkspacePersistenceManager(paths.state_db)
        await manager.open()
        store = SqliteSessionStore(manager.connection)
        await store.save(
            SessionInfo(
                session_id=session_id,
                agent_name="default",
                created_at=1000,
                updated_at=2000,
            )
        )
        await manager.close()

        async def _session_store_resolver(_index_dir: Path):
            m = WorkspacePersistenceManager(paths.state_db)
            await m.open()
            return SqliteSessionStore(m.connection)

        gc = SessionGarbageCollector(
            workspace_roots_provider=lambda: [tmp_path],
            data_dir_name=".modex",
            config=SessionGcConfig(max_workers=1),
            cleaner_factory=SessionCleanerFactory(
                backend=PersistenceBackend.SQLITE,
                persistence_resolver=lambda _root: None,
            ),
            session_store_resolver=_session_store_resolver,
        )

        await gc.delete_session_tree(session_id, ws_root=tmp_path, pool=pool)

        verify = WorkspacePersistenceManager(paths.state_db)
        await verify.open()
        count = await verify.connection.query_value(
            "SELECT COUNT(*) FROM sessions WHERE session_id = ?",
            int,
            (session_id,),
        )
        await verify.close()
        return count

    assert asyncio.run(_run()) == 0


def test_sqlite_delete_session_tree_removes_transcript_events(tmp_path) -> None:
    """delete_session_tree must synchronously remove transcript events."""
    from bot.service.session_cleaner_factory import SessionCleanerFactory

    from modex_agent.persistence.adapters.session_store import SqliteSessionStore
    from modex_agent.persistence.config import PersistenceBackend
    from modex_agent.persistence.managers import WorkspacePersistenceManager

    paths = _paths_for(tmp_path)
    session_id = "f827db2b9945.default"
    pool = "default"

    async def _run() -> int:
        manager = WorkspacePersistenceManager(paths.state_db)
        await manager.open()
        await manager.connection.execute(
            "CREATE TABLE IF NOT EXISTS bot_webui_transcript_events ("
            "event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, "
            "session_prefix TEXT NOT NULL, "
            "pool_name TEXT NOT NULL, "
            "agent_name TEXT NOT NULL, "
            "event_type TEXT NOT NULL, "
            "turn_id TEXT, "
            "timestamp_ms INTEGER NOT NULL, "
            "payload_json TEXT NOT NULL)"
        )
        await manager.connection.execute(
            "INSERT INTO bot_webui_transcript_events "
            "(session_id, session_prefix, pool_name, agent_name, event_type, "
            "timestamp_ms, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "f827db2b9945",
                pool,
                "default",
                "user_message",
                1000,
                "{}",
            ),
        )
        await manager.close()

        class _FakeTranscriptStore:
            async def delete_session(self, sid, sessions_dir=None) -> None:
                m = WorkspacePersistenceManager(paths.state_db)
                await m.open()
                await m.connection.execute(
                    "DELETE FROM bot_webui_transcript_events WHERE session_id = ?",
                    (sid,),
                )
                await m.close()

        async def _session_store_resolver(_index_dir: Path):
            m = WorkspacePersistenceManager(paths.state_db)
            await m.open()
            return SqliteSessionStore(m.connection)

        gc = SessionGarbageCollector(
            workspace_roots_provider=lambda: [tmp_path],
            data_dir_name=".modex",
            config=SessionGcConfig(max_workers=1),
            cleaner_factory=SessionCleanerFactory(
                backend=PersistenceBackend.SQLITE,
                persistence_resolver=lambda _root: None,
            ),
            transcript_store=_FakeTranscriptStore(),  # type: ignore[arg-type]
            session_store_resolver=_session_store_resolver,
        )

        await gc.delete_session_tree(session_id, ws_root=tmp_path, pool=pool)

        verify = WorkspacePersistenceManager(paths.state_db)
        await verify.open()
        count = await verify.connection.query_value(
            "SELECT COUNT(*) FROM bot_webui_transcript_events WHERE session_id = ?",
            int,
            (session_id,),
        )
        await verify.close()
        return count

    assert asyncio.run(_run()) == 0
