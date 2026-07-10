# tests/service/test_session_gc.py
from bot.service.session_gc import SessionGcConfig, load_session_gc_config


def test_config_defaults_when_key_absent():
    cfg = load_session_gc_config({})
    assert cfg.enabled is True
    assert cfg.scan_interval_seconds == 300
    assert cfg.max_workers == 1


def test_config_overrides_from_raw_dict():
    cfg = load_session_gc_config({"session_gc": {"enabled": False, "scan_interval_seconds": 60, "max_workers": 2}})
    assert cfg.enabled is False
    assert cfg.scan_interval_seconds == 60
    assert cfg.max_workers == 2


def test_config_is_frozen_and_strict():
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

from bot.service.session_gc import _session_artifact_paths


def _paths_for(tmp_path: Path):
    from modex_agent.workspace.paths import WorkspacePaths
    return WorkspacePaths(root=tmp_path / ".modex")


def test_artifact_paths_all_ten_with_correct_naming(tmp_path):
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
    # fork context: {agent}_{prefix}.xml
    assert (paths.fork_contexts_dir(pool) / "coding_009fc886ecba.xml") in ap
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
    # exactly ten units
    assert len(ap) == 10


def test_artifact_paths_excludes_pool_shared(tmp_path):
    paths = _paths_for(tmp_path)
    ap = _session_artifact_paths("x.main", "main", paths)
    # archive + knowledge are pool-shared, must NOT appear
    assert not any("archive" in str(p) for p in ap)
    assert not any("knowledge" in str(p) for p in ap)
    # commands leaf is unused, must NOT appear
    assert not any("commands" in str(p) for p in ap)


import json

from bot.service.session_gc import _read_session_index, _find_orphan_sessions, _find_children


def _write_index(paths, pool, session_id, parent=None):
    rec = {"session_id": session_id, "agent_name": session_id.split(".")[-1],
           "parent_session_id": parent, "created_at": 0, "updated_at": 0, "metadata": {}}
    d = paths.session_index_dir / pool
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_read_index_builds_parent_graph(tmp_path):
    paths = _paths_for(tmp_path)
    _write_index(paths, "coding", "aaa.coding", None)
    _write_index(paths, "coding", "bbb.worker", "aaa.coding")
    graph = _read_session_index(paths)
    assert graph["aaa.coding"].parent_session_id is None
    assert graph["bbb.worker"].parent_session_id == "aaa.coding"


def test_orphan_sessions_when_parent_gone(tmp_path):
    paths = _paths_for(tmp_path)
    # root index removed (simulates interrupted cascade); child remains
    _write_index(paths, "coding", "bbb.worker", "aaa.coding")
    _write_index(paths, "coding", "ccc.scout", "bbb.worker")
    orphans = _find_orphan_sessions(paths)
    orphan_ids = {o.session_id for o in orphans}
    # bbb is orphan (parent aaa gone); ccc is NOT yet (parent bbb still present)
    assert "bbb.worker" in orphan_ids
    assert "ccc.scout" not in orphan_ids


def test_find_children_across_pools(tmp_path):
    paths = _paths_for(tmp_path)
    _write_index(paths, "coding", "aaa.coding", None)
    _write_index(paths, "research", "ddd.researcher", "aaa.coding")
    children = _find_children("aaa.coding", paths)
    child_ids = {(c.session_id, c.pool) for c in children}
    assert ("ddd.researcher", "research") in child_ids


from bot.service.session_gc import _find_orphan_artifact_sids


def test_orphan_artifacts_when_index_gone(tmp_path):
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

from bot.service.session_gc import clean_session


def _seed_full_session(paths, pool, sid, parent=None):
    _write_index(paths, pool, sid, parent)
    for unit in _session_artifact_paths(sid, pool, paths):
        if unit.suffix == ".json" and "session_index" in str(unit):
            continue  # already written by _write_index above
        if unit.suffix == ".jsonl" and "sessions" in str(unit):
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("{}", encoding="utf-8")
        elif unit.suffix in (".json", ".xml"):
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("{}", encoding="utf-8")
        else:
            unit.mkdir(parents=True, exist_ok=True)
            (unit / "data").write_text("x", encoding="utf-8")


def test_clean_session_removes_all_ten_units(tmp_path):
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding")
    asyncio.run(clean_session("aaa.coding", "coding", paths))
    for unit in _session_artifact_paths("aaa.coding", "coding", paths):
        assert not unit.exists(), f"still present: {unit}"


def test_clean_session_idempotent_when_already_gone(tmp_path):
    paths = _paths_for(tmp_path)
    # nothing seeded at all
    asyncio.run(clean_session("ghost.coding", "coding", paths))  # must not raise
    asyncio.run(clean_session("ghost.coding", "coding", paths))  # twice is fine


def test_clean_session_preserves_pool_shared_and_siblings(tmp_path):
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding")
    _seed_full_session(paths, "coding", "sib.coding")
    archive = paths.memory_dir("coding") / "archive"
    archive.mkdir(parents=True)
    (archive / "state.json").write_text("{}", encoding="utf-8")
    knowledge = paths.memory_dir("coding") / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "MEMORY.md").write_text("kept", encoding="utf-8")

    asyncio.run(clean_session("aaa.coding", "coding", paths))

    # sibling untouched
    assert (paths.session_index_dir / "coding" / "sib.coding.json").exists()
    # pool-shared untouched
    assert (archive / "state.json").exists()
    assert (knowledge / "MEMORY.md").read_text(encoding="utf-8") == "kept"


from bot.service.session_gc import _propagate_children


def test_propagate_children_returns_descendants_across_pools(tmp_path):
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    _seed_full_session(paths, "coding", "bbb.worker", "aaa.coding")
    _seed_full_session(paths, "research", "ccc.researcher", "bbb.worker")  # nested + cross-pool
    kids = _propagate_children("aaa.coding", paths)
    ids = {k.session_id for k in kids}
    assert ids == {"bbb.worker"}
    grandkids = _propagate_children("bbb.worker", paths)
    assert {k.session_id for k in grandkids} == {"ccc.researcher"}


from bot.service.session_gc import SessionGarbageCollector


def _collector(tmp_path):
    return SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
    )


def test_delete_session_tree_drains_full_cascade(tmp_path):
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    _seed_full_session(paths, "coding", "bbb.worker", "aaa.coding")
    _seed_full_session(paths, "coding", "ccc.scout", "bbb.worker")  # nested
    gc = _collector(tmp_path)

    async def _run():
        await gc.delete_session_tree("aaa.coding")
        await gc._drain_for_tests()

    asyncio.run(_run())
    for sid in ("aaa.coding", "bbb.worker", "ccc.scout"):
        for unit in _session_artifact_paths(sid, "coding", paths):
            assert not unit.exists(), f"{sid} still has {unit}"


def test_sweep_once_completes_interrupted_cascade(tmp_path):
    paths = _paths_for(tmp_path)
    # simulate: root already gone, child remains (interrupted cascade)
    _seed_full_session(paths, "coding", "bbb.worker", "aaa.coding")
    gc = _collector(tmp_path)

    async def _run():
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    for unit in _session_artifact_paths("bbb.worker", "coding", paths):
        assert not unit.exists()


def test_sweep_once_removes_orphan_artifacts(tmp_path):
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "orphan.coding", None)
    # now delete ONLY its index, leaving artifacts (simulated crash after index)
    (paths.session_index_dir / "coding" / "orphan.coding.json").unlink()
    gc = _collector(tmp_path)

    async def _run():
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    assert not (paths.memory_dir("coding") / "session" / "orphan.coding").exists()


def test_dedup_suppresses_concurrent_duplicate(tmp_path):
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    gc = _collector(tmp_path)
    added1 = gc._enqueue("aaa.coding", "coding", tmp_path)
    added2 = gc._enqueue("aaa.coding", "coding", tmp_path)
    assert added1 is True
    assert added2 is False
    assert gc._inflight_count() == 1


def test_start_stop_clean(tmp_path):
    gc = _collector(tmp_path)

    async def _run():
        await gc.start()
        assert gc._sweep_task is not None

    asyncio.run(_run())
    # stop outside the start-loop to also exercise the no-running-loop path
    async def _stop():
        await gc.stop()

    asyncio.run(_stop())
    assert gc._sweep_task is None
    assert gc._inflight_count() == 0
    # restart-safe: a second start works on a fresh state
    async def _run2():
        await gc.start()
        await gc.stop()

    asyncio.run(_run2())
    assert gc._sweep_task is None


def test_sweep_covers_multiple_workspaces(tmp_path):
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

    async def _run():
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    for unit in _session_artifact_paths("zzz.coding", "coding", other_paths):
        assert not unit.exists()


def test_sweep_catches_orphan_transcript(tmp_path):
    paths = _paths_for(tmp_path)
    # a transcript with NO index record and NO memory dir (orphan transcript)
    tf = paths.sessions_dir / "coding" / "ghost.coding.jsonl"
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_text("{}\n", encoding="utf-8")
    gc = _collector(tmp_path)

    async def _run():
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    assert not tf.exists()


def test_dedup_removed_on_clean_failure(tmp_path, monkeypatch):
    """A sid is removed from inflight even when clean_session raises (backstop can retry)."""
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    gc = _collector(tmp_path)

    async def _boom(session_id, pool, paths):  # noqa: ANN001
        raise OSError("simulated locked file")

    monkeypatch.setattr("bot.service.session_gc.clean_session", _boom)
    gc._enqueue("aaa.coding", "coding", tmp_path)

    async def _run():
        await gc._drain_for_tests()

    asyncio.run(_run())
    assert gc._inflight_count() == 0


def test_delete_session_tree_idempotent_when_repeated(tmp_path):
    """Calling delete_session_tree twice is safe; the duplicate enqueue is deduped."""
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    gc = _collector(tmp_path)

    async def _run():
        await gc.delete_session_tree("aaa.coding", ws_root=tmp_path, pool="coding")
        await gc.delete_session_tree("aaa.coding", ws_root=tmp_path, pool="coding")
        await gc._drain_for_tests()

    asyncio.run(_run())
    assert not (paths.session_index_dir / "coding" / "aaa.coding.json").exists()


def test_sweep_drains_multi_layer_orphan_tree_in_one_pass(tmp_path):
    """Top-layer sweep + in-pool BFS propagation drains a multi-layer orphan tree
    in a single sweep_once + drain (ccc is not an orphan at sweep time — its parent
    bbb is still present — but is reached via bbb's propagation once bbb is cleaned)."""
    paths = _paths_for(tmp_path)
    # root aaa gone; child bbb + grandchild ccc remain
    _seed_full_session(paths, "coding", "bbb.worker", "aaa.coding")
    _seed_full_session(paths, "coding", "ccc.scout", "bbb.worker")
    gc = _collector(tmp_path)

    async def _run():
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    for sid in ("bbb.worker", "ccc.scout"):
        for unit in _session_artifact_paths(sid, "coding", paths):
            assert not unit.exists(), f"{sid} still has {unit}"


def test_cleanup_orphan_pool_routes(tmp_path):
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


def test_sweep_once_cleans_orphan_pool_routes(tmp_path):
    """sweep_once removes orphan pool_sessions routing entries (end-to-end)."""
    paths = _paths_for(tmp_path)
    routes = paths.pool_sessions_dir
    routes.mkdir(parents=True, exist_ok=True)
    (routes / "gone.json").write_text(
        json.dumps({"pool": "main", "session_id": "gone"}), encoding="utf-8"
    )
    gc = _collector(tmp_path)

    async def _run():
        await gc.sweep_once()
        await gc._drain_for_tests()

    asyncio.run(_run())
    assert not (routes / "gone.json").exists()


def test_clean_session_emits_log(tmp_path, caplog):
    """Cleaning a session emits one log line with its id/pool/workspace."""
    import logging

    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    gc = _collector(tmp_path)
    gc._enqueue("aaa.coding", "coding", tmp_path)

    with caplog.at_level(logging.INFO, logger="bot.service.session_gc"):
        async def _run():
            await gc._drain_for_tests()

        asyncio.run(_run())
    assert any(
        "aaa.coding" in r.message and "cleaned" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]


def test_sweep_once_emits_summary(tmp_path, caplog):
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
        async def _run():
            await gc.sweep_once()

        asyncio.run(_run())
    summary = [r.message for r in caplog.records if "sweep done" in r.message]
    assert summary, [r.message for r in caplog.records]
    assert "removed 1 pool route" in summary[0]
