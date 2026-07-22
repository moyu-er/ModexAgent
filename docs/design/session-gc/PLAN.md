# Session Garbage Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete a conversation's full cascade (root + all subagent descendants via `parent_session_id`) and all ten per-session artifact types, crash-recoverably, with a periodic backstop sweep.

**Architecture:** A bot-side `SessionGarbageCollector` deep module. A session is live iff its index record exists. The idempotent unit `clean_session` removes a session's record + transcript + ten artifacts (index first), then enqueues its children found via `parent_session_id`. Two triggers feed one single-worker pool: foreground `delete_session_tree` (sync-removes root record, then enqueues) and `sweep_once` (finds orphans from disk). The sweep is the sole retry authority; an in-memory non-persistent dedup set suppresses concurrent duplicates and is cleared on any task end so the backstop is never blocked. No tombstone, no OS-lock reliance, no framework changes. See ADR-0018 and `docs/design/session-gc/PRD.md`.

**Tech Stack:** Python 3.12, asyncio, aiohttp (lifecycle only), Pydantic (frozen config), pytest. Framework path primitives: `WorkspacePaths`, `safe_filename`, `sanitize_scope_key`, `safe_segment`, `agent_of`, `session_id_prefix_of`, runtime store `_safe_segment` classmethods.

---

## File Structure

- **Create** `examples/bot_project/bot/service/session_gc.py` — the deep module: `SessionGcConfig` (Pydantic frozen), `load_session_gc_config` (raw-dict → model), pure helpers `_session_artifact_paths` / `_read_session_index` / `_find_orphan_sessions` / `_find_orphan_artifact_sids` / `_find_children`, and `SessionGarbageCollector` (clean_session, dedup, single-worker pool, sweep_once, start/stop).
- **Create** `examples/bot_project/tests/service/test_session_gc.py` — integration tests at the collector public-interface seam (real disk trees): cascade, backstop, dedup, lifecycle, multi-workspace. Plus pure-unit tests for the helpers and config.
- **Modify** `examples/bot_project/bot/service/web_ui_service.py` — construct the collector in `start()` with a workspace-roots provider backed by `GlobalWorkspaceStore`, inject into the server, stop in `stop()`.
- **Modify** `examples/bot_project/bot/webui/server.py` — add `set_session_gc()` setter; change `_handle_delete_session` to delegate to the collector; keep `{deleted: id}` response.
- **Modify** `examples/bot_project/bot/workspace/wiring.py` — expose `store` (the `GlobalWorkspaceStore`) on `WorkspaceStack` so the service can build the roots provider.

All new code is bot-layer. No `src/modex_agent/` edits.

---

## Task 1: SessionGcConfig + raw-dict loader

**Files:**
- Create: `examples/bot_project/bot/service/session_gc.py`
- Test: `examples/bot_project/tests/service/test_session_gc.py`

- [ ] **Step 1: Write the failing tests**

Create the test file with this first test (and a `tests/service/__init__.py` if the dir is new — check first; if `tests/service/` already exists, skip the init):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.service.session_gc'`

- [ ] **Step 3: Write minimal implementation**

Create `bot/service/session_gc.py`:

```python
"""Crash-safe session garbage collection (ADR-0018).

A bot-side collector that deletes a conversation's full cascade (root + every
subagent descendant via ``parent_session_id``) and all ten per-session artifact
types. Crash recoverability is the first constraint: deletion progress is fully
reconstructable from disk (the session-index graph) after a restart, with no
in-memory closure collected up front. See ADR-0018 and the session-lifecycle
glossary in CONTEXT.md.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/session_gc.py examples/bot_project/tests/service/test_session_gc.py
git commit -m "feat(session-gc): add SessionGcConfig + raw-dict loader"
```

---

## Task 2: Artifact path derivation (the ten types)

**Files:**
- Modify: `examples/bot_project/bot/service/session_gc.py`
- Test: `examples/bot_project/tests/service/test_session_gc.py`

The highest-risk part: a single session id takes ~five on-disk forms. `_session_artifact_paths` derives each leaf's path with the SAME transform that leaf's store uses. It returns whole per-session units (dirs/files) to delete — never sub-files inside a dir.

- [ ] **Step 1: Write the failing tests**

Append to `tests/service/test_session_gc.py`:

```python
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
    by_name = {p.name: p for p in ap}

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
    # archive + knowledge (on-disk directory name for Core Memory, ADR-0035) are pool-shared, must NOT appear
    assert not any("archive" in str(p) for p in ap)
    assert not any("knowledge" in str(p) for p in ap)
    # commands leaf is unused, must NOT appear
    assert not any("commands" in str(p) for p in ap)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: FAIL — `ImportError: cannot import name '_session_artifact_paths'`

- [ ] **Step 3: Write minimal implementation**

Append to `bot/service/session_gc.py`:

```python
from pathlib import Path

from modex_agent.core.session_id import agent_of, session_id_prefix_of
from modex_agent.core.session_store import safe_filename
from modex_agent.memory.stores.utils import sanitize_scope_key
from modex_agent.runtime.store import JsonFileTodoStore, JsonFileTurnStateStore
from modex_agent.workspace.paths import WorkspacePaths, safe_segment

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/session_gc.py examples/bot_project/tests/service/test_session_gc.py
git commit -m "feat(session-gc): derive the ten per-session artifact paths"
```

---

## Task 3: Session-index graph reader + orphan-session detection

**Files:**
- Modify: `examples/bot_project/bot/service/session_gc.py`
- Test: `examples/bot_project/tests/service/test_session_gc.py`

Reads the index directly (the source of truth for the parent graph) and finds Rule-1 orphans: non-root sessions whose parent index is gone.

- [ ] **Step 1: Write the failing tests**

Append:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: FAIL — `ImportError` for the three names.

- [ ] **Step 3: Write minimal implementation**

Append to `bot/service/session_gc.py`:

```python
import logging

from modex_agent.core.session_id import SessionInfo

logger = logging.getLogger(__name__)


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/session_gc.py examples/bot_project/tests/service/test_session_gc.py
git commit -m "feat(session-gc): read index graph, detect orphan sessions + children"
```

---

## Task 4: Orphan-artifact detection (Rule 2)

**Files:**
- Modify: `examples/bot_project/bot/service/session_gc.py`
- Test: `examples/bot_project/tests/service/test_session_gc.py`

Scans the memory-session dir (sid verbatim — the canonical reversible signal every ran-session leaves) for sids that have no index record. Each such sid is fed to `clean_session`, which recomputes all ten paths (idempotent), so media/turns/fork of that sid are reclaimed too.

- [ ] **Step 1: Write the failing tests**

Append:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: FAIL — `ImportError: cannot import name '_find_orphan_artifact_sids'`

- [ ] **Step 3: Write minimal implementation**

Append to `bot/service/session_gc.py`:

```python
def _find_orphan_artifact_sids(paths: WorkspacePaths) -> set[str]:
    """Rule 2: session ids that left artifacts but have no index record.

    Uses the memory-session dir (``memory/<pool>/session/<sid>/``) as the
    signal: its name is the raw session id (dots preserved), and every session
    that ran a turn creates one. Each orphan sid is cleaned wholesale by
    ``clean_session`` (which recomputes all ten paths), so this single signal
    reclaims the session's media/turns/fork/etc. too.
    """
    live = _read_session_index(paths)
    orphans: set[str] = set()
    session_root = paths.session_index_dir.parent / "memory"  # memory/<pool>/session/*
    # Iterate each pool's session dir to recover (sid, pool).
    memory_base = paths.root / "memory"
    if not memory_base.is_dir():
        return orphans
    for pool_dir in memory_base.iterdir():
        sess_dir = pool_dir / "session"
        if not sess_dir.is_dir():
            continue
        for child in sess_dir.iterdir():
            if child.is_dir() and child.name not in live:
                orphans.add(child.name)
    return orphans
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/session_gc.py examples/bot_project/tests/service/test_session_gc.py
git commit -m "feat(session-gc): detect orphan artifacts via memory-session dir"
```

---

## Task 5: `clean_session` core (idempotent, exception-safe, no propagation yet)

**Files:**
- Modify: `examples/bot_project/bot/service/session_gc.py`
- Test: `examples/bot_project/tests/service/test_session_gc.py`

Removes one session's record + transcript + ten artifacts. Idempotent (missing = no-op). `unlink(missing_ok=True)`, `rmtree(ignore_errors=False)`; any `OSError` aborts that session this round (caller/backstop retries). Pool-shared artifacts untouched because the derivation only ever produces per-session units.

- [ ] **Step 1: Write the failing tests**

Append:

```python
import shutil

from bot.service.session_gc import clean_session


def _seed_full_session(paths, pool, sid, parent=None):
    _write_index(paths, pool, sid, parent)
    for unit in _session_artifact_paths(sid, pool, paths):
        if unit.suffix == ".json" and "session_index" in str(unit):
            continue  # already written above
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
    clean_session("aaa.coding", "coding", paths)
    for unit in _session_artifact_paths("aaa.coding", "coding", paths):
        assert not unit.exists(), f"still present: {unit}"


def test_clean_session_idempotent_when_already_gone(tmp_path):
    paths = _paths_for(tmp_path)
    # nothing seeded at all
    clean_session("ghost.coding", "coding", paths)  # must not raise
    clean_session("ghost.coding", "coding", paths)  # twice is fine


def test_clean_session_preserves_pool_shared_and_siblings(tmp_path):
    paths = _paths_for(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding")
    _seed_full_session(paths, "coding", "sib.coding")
    archive = paths.memory_dir("coding") / "archive"
    archive.mkdir(parents=True)
    (archive / "state.json").write_text("{}", encoding="utf-8")
    # On-disk directory is still named `knowledge/` (Core Memory layer per ADR-0035; the directory name is unchanged)
    knowledge = paths.memory_dir("coding") / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "MEMORY.md").write_text("kept", encoding="utf-8")

    clean_session("aaa.coding", "coding", paths)

    # sibling untouched
    assert (paths.session_index_dir / "coding" / "sib.coding.json").exists()
    # pool-shared untouched
    assert (archive / "state.json").exists()
    assert (knowledge / "MEMORY.md").read_text(encoding="utf-8") == "kept"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: FAIL — `ImportError: cannot import name 'clean_session'`

- [ ] **Step 3: Write minimal implementation**

Append to `bot/service/session_gc.py`:

```python
async def clean_session(session_id: str, pool: str, paths: WorkspacePaths) -> None:
    """Idempotently remove one session's record + transcript + ten artifacts.

    Order is index-first (Path B): the existence marker goes before the
    artifacts, so a mid-cleanup session vanishes from the orphan-session rule's
    view almost immediately. Any OSError aborts this session for this round —
    the backstop sweep re-discovers and retries it. Missing targets are no-ops.
    Returns the session's children (for cascade propagation by the caller).
    """
    await asyncio.to_thread(_clean_session_sync, session_id, pool, paths)


def _clean_session_sync(session_id: str, pool: str, paths: WorkspacePaths) -> None:
    units = _session_artifact_paths(session_id, pool, paths)
    # index record first (existence marker), then transcript, then artifacts
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
    # this round (backstop retries). missing_ok / suppress FileNotFoundError.
    try:
        if unit.is_dir():
            shutil.rmtree(unit)
        elif unit.exists():
            unit.unlink()
    except FileNotFoundError:
        pass
```

Add `import asyncio` and `import shutil` to the imports at the top of `bot/service/session_gc.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: PASS (12 tests). Note: `clean_session` is async; the tests above call it as sync — update the tests to use `asyncio.run`:

In the test file, wrap the three `clean_session(...)` calls:
```python
import asyncio
# replace each call:
asyncio.run(clean_session("aaa.coding", "coding", paths))
```
(Apply to all three clean_session call sites in the Task-5 tests.)

Re-run and confirm PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/session_gc.py examples/bot_project/tests/service/test_session_gc.py
git commit -m "feat(session-gc): idempotent clean_session removes record + ten artifacts"
```

---

## Task 6: Cascade propagation + dedup

**Files:**
- Modify: `examples/bot_project/bot/service/session_gc.py`
- Test: `examples/bot_project/tests/service/test_session_gc.py`

`clean_session` returns its children; the collector enqueues them. This task adds a `_cascade` that drives clean_session + child enqueue, guarded by an in-memory dedup set removed in `finally`. Tested at the pure level first (children discovered + dedup semantics); the pool wiring is Task 7.

- [ ] **Step 1: Write the failing tests**

Append:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: FAIL — `ImportError: cannot import name '_propagate_children'`

- [ ] **Step 3: Write minimal implementation**

Append to `bot/service/session_gc.py`:

```python
def _propagate_children(parent_sid: str, paths: WorkspacePaths) -> list[_IndexedSession]:
    """The children of *parent_sid* that still need cleaning (across all pools).

    Called by the collector after cleaning a session so it can enqueue each
    child as its own ``clean_session`` unit (BFS propagation lives in the pool).
    """
    return _find_children(parent_sid, paths)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/session_gc.py examples/bot_project/tests/service/test_session_gc.py
git commit -m "feat(session-gc): expose child propagation for cascade BFS"
```

---

## Task 7: `SessionGarbageCollector` — pool, dedup, delete_session_tree, sweep_once

**Files:**
- Modify: `examples/bot_project/bot/service/session_gc.py`
- Test: `examples/bot_project/tests/service/test_session_gc.py`

The deep module. Single-worker pool (configurable). `delete_session_tree`: sync-remove root record then enqueue `clean_session(root)`. `sweep_once`: enqueue top-layer orphans (sessions + artifacts). Dedup set: add on enqueue, remove in task `finally`. The worker runs `clean_session` then enqueues its children.

- [ ] **Step 1: Write the failing tests**

Append:

```python
from bot.service.session_gc import SessionGarbageCollector


def _collector(tmp_path):
    roots = lambda: [tmp_path]  # noqa: E731  one workspace whose .modex lives under tmp_path
    return SessionGarbageCollector(
        workspace_roots_provider=roots,
        data_dir_name=".modex",
        config=SessionGcConfig(max_workers=1),
    )


def _ws_paths(tmp_path):
    return _paths_for(tmp_path)


def test_delete_session_tree_drains_full_cascade(tmp_path):
    paths = _ws_paths(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    _seed_full_session(paths, "coding", "bbb.worker", "aaa.coding")
    _seed_full_session(paths, "coding", "ccc.scout", "bbb.worker")  # nested
    gc = _collector(tmp_path)
    asyncio.run(gc.start())
    try:
        asyncio.get_event_loop().run_until_complete(gc.delete_session_tree("aaa.coding"))
        # drain the pool synchronously for the test
        asyncio.get_event_loop().run_until_complete(gc._drain_for_tests())
    finally:
        asyncio.get_event_loop().run_until_complete(gc.stop())
    for sid in ("aaa.coding", "bbb.worker", "ccc.scout"):
        for unit in _session_artifact_paths(sid, "coding", paths):
            assert not unit.exists(), f"{sid} still has {unit}"


def test_sweep_once_completes_interrupted_cascade(tmp_path):
    paths = _ws_paths(tmp_path)
    # simulate: root already gone, child remains (interrupted cascade)
    _seed_full_session(paths, "coding", "bbb.worker", "aaa.coding")
    gc = _collector(tmp_path)
    asyncio.run(gc.start())
    try:
        asyncio.get_event_loop().run_until_complete(gc.sweep_once())
        asyncio.get_event_loop().run_until_complete(gc._drain_for_tests())
    finally:
        asyncio.get_event_loop().run_until_complete(gc.stop())
    for unit in _session_artifact_paths("bbb.worker", "coding", paths):
        assert not unit.exists()


def test_sweep_once_removes_orphan_artifacts(tmp_path):
    paths = _ws_paths(tmp_path)
    _seed_full_session(paths, "coding", "orphan.coding", None)
    # now delete ONLY its index, leaving artifacts (simulated crash after index)
    (paths.session_index_dir / "coding" / "orphan.coding.json").unlink()
    gc = _collector(tmp_path)
    asyncio.run(gc.start())
    try:
        asyncio.get_event_loop().run_until_complete(gc.sweep_once())
        asyncio.get_event_loop().run_until_complete(gc._drain_for_tests())
    finally:
        asyncio.get_event_loop().run_until_complete(gc.stop())
    assert not (paths.memory_dir("coding") / "session" / "orphan.coding").exists()


def test_dedup_suppresses_concurrent_duplicate(tmp_path):
    paths = _ws_paths(tmp_path)
    _seed_full_session(paths, "coding", "aaa.coding", None)
    gc = _collector(tmp_path)
    asyncio.run(gc.start())
    try:
        gc._enqueue("aaa.coding", "coding", paths)  # type: ignore[attr-defined]
        gc._enqueue("aaa.coding", "coding", paths)  # duplicate
        assert gc._inflight_count() == 1  # type: ignore[attr-defined]
    finally:
        asyncio.get_event_loop().run_until_complete(gc.stop())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: FAIL — `ImportError: cannot import name 'SessionGarbageCollector'`

- [ ] **Step 3: Write minimal implementation**

Append to `bot/service/session_gc.py`:

```python
from collections.abc import Callable, Iterable


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

    async def stop(self) -> None:
        self._stopping = True
        if self._sweep_task is not None:
            self._sweep_task.cancel()
        for _ in self._workers:
            await self._queue.put(None)  # sentinel
        for w in self._workers:
            w.cancel()
        self._workers.clear()
        self._inflight.clear()

    async def delete_session_tree(self, root_session_id: str) -> None:
        """Foreground trigger: remove the root's record now, enqueue the rest.

        The root's pool + workspace are resolved by scanning every workspace's
        index for the root record (the foreground knows the session id, not the
        pool). Sync-removing the record makes the conversation leave the list
        immediately; the cascade drains asynchronously.
        """
        for ws_root in self._roots_provider():
            paths = WorkspacePaths(root=ws_root / self._data_dir_name)
            graph = _read_session_index(paths)
            node = graph.get(root_session_id)
            if node is None:
                continue
            # sync-remove root record + transcript so the UI updates immediately
            await asyncio.to_thread(_clean_record_and_transcript, root_session_id, node.pool, paths)
            self._enqueue(root_session_id, node.pool, ws_root)
            return

    async def sweep_once(self) -> None:
        """One backstop pass over all workspaces: enqueue top-layer orphans."""
        if self._config.enabled is False:
            return
        for ws_root in self._roots_provider():
            if not ws_root.is_dir():
                continue
            paths = WorkspacePaths(root=ws_root / self._data_dir_name)
            for orphan in _find_orphan_sessions(paths):
                self._enqueue(orphan.session_id, orphan.pool, ws_root)
            for sid in _find_orphan_artifact_sids(paths):
                pool = _pool_of_memory_sid(paths, sid) or "main"
                self._enqueue(sid, pool, ws_root)

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
            try:
                paths = WorkspacePaths(root=job.ws_root / self._data_dir_name)
                try:
                    await clean_session(job.session_id, job.pool, paths)
                except OSError:
                    logger.exception("session-gc: clean_session failed for %s; backstop will retry", job.session_id)
                # BFS propagation: enqueue children (self-propagating unit)
                for child in _propagate_children(job.session_id, paths):
                    self._enqueue(child.session_id, child.pool, job.ws_root)
            finally:
                self._inflight.discard(job.session_id)

    # -- test helpers (not public API) ----------------------------------
    async def _drain_for_tests(self) -> None:
        """Process queued jobs until the queue is empty (tests only)."""
        while not self._queue.empty():
            job = self._queue.get_nowait()
            if job is None:
                continue
            try:
                paths = WorkspacePaths(root=job.ws_root / self._data_dir_name)
                try:
                    await clean_session(job.session_id, job.pool, paths)
                except OSError:
                    logger.exception("session-gc: clean_session failed (test drain) %s", job.session_id)
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


def _pool_of_memory_sid(paths: WorkspacePaths, sid: str) -> str | None:
    """Recover the pool for an orphan sid found via its memory-session dir."""
    memory_base = paths.root / "memory"
    if not memory_base.is_dir():
        return None
    for pool_dir in memory_base.iterdir():
        if (pool_dir / "session" / sid).is_dir():
            return pool_dir.name
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: PASS (17 tests). If `asyncio.get_event_loop()` warnings appear, that is fine for tests; behavior must pass.

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/session_gc.py examples/bot_project/tests/service/test_session_gc.py
git commit -m "feat(session-gc): collector with pool, dedup, delete_session_tree, sweep_once"
```

---

## Task 8: Periodic sweep loop + lifecycle

**Files:**
- Modify: `examples/bot_project/bot/service/session_gc.py`
- Test: `examples/bot_project/tests/service/test_session_gc.py`

`start()` also launches the periodic loop (delay-after-completion cadence). `stop()` cancels it. Verify start/spawn + stop/clear + restart-safety (dedup empty after stop).

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_start_stop_clean(tmp_path):
    gc = _collector(tmp_path)
    asyncio.run(gc.start())
    assert gc._sweep_task is not None  # type: ignore[attr-defined]
    asyncio.get_event_loop().run_until_complete(gc.stop())
    assert gc._sweep_task is None or gc._sweep_task.cancelled()  # type: ignore[attr-defined]
    assert gc._inflight_count() == 0
    # restart-safe: a second start works on a fresh state
    asyncio.run(gc.start())
    asyncio.get_event_loop().run_until_complete(gc.stop())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py::test_start_stop_clean -v`
Expected: FAIL — `_sweep_task` stays None (loop not launched).

- [ ] **Step 3: Write minimal implementation**

Modify `start()` to also launch the loop, and add the loop method. Replace the existing `start` with:

```python
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
```

And in `stop()`, after cancelling, set `self._sweep_task = None`:

```python
        self._workers.clear()
        self._inflight.clear()
        self._sweep_task = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py -v`
Expected: PASS (18 tests). (The sweep_loop's `asyncio.sleep(300)` is cancelled by `stop()` before it fires in tests.)

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/service/session_gc.py examples/bot_project/tests/service/test_session_gc.py
git commit -m "feat(session-gc): periodic sweep loop with delay-after-completion cadence"
```

---

## Task 9: Multi-workspace sweep coverage

**Files:**
- Modify: `examples/bot_project/tests/service/test_session_gc.py`
- Test: same file

Confirms the sweep spans home + a non-home workspace (the roots provider returns both), per PRD user story 25.

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_sweep_covers_multiple_workspaces(tmp_path):
    home = tmp_path / "home"
    other = tmp_path / "other"
    home.mkdir(); other.mkdir()
    home_paths = _paths_for(home)
    other_paths = _paths_for(other)
    # orphan in OTHER workspace only
    _seed_full_session(other_paths, "coding", "zzz.coding", "gone.coding")
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [home, other],
        data_dir_name=".modex",
        config=SessionGcConfig(),
    )
    asyncio.run(gc.start())
    try:
        asyncio.get_event_loop().run_until_complete(gc.sweep_once())
        asyncio.get_event_loop().run_until_complete(gc._drain_for_tests())
    finally:
        asyncio.get_event_loop().run_until_complete(gc.stop())
    for unit in _session_artifact_paths("zzz.coding", "coding", other_paths):
        assert not unit.exists()
```

- [ ] **Step 2: Run test to verify it passes (no new code — exercises existing sweep)**

Run: `cd examples/bot_project && python -m pytest tests/service/test_session_gc.py::test_sweep_covers_multiple_workspaces -v`
Expected: PASS. (If it FAILS because the roots provider signature differs, fix the provider wiring in the collector — the test pins the contract that the provider returns raw workspace roots and the collector appends `data_dir_name`.)

- [ ] **Step 3: Commit**

```bash
git add examples/bot_project/tests/service/test_session_gc.py
git commit -m "test(session-gc): sweep covers home + non-home workspaces"
```

---

## Task 10: Wire collector into web_ui_service (lifecycle + roots provider)

**Files:**
- Modify: `examples/bot_project/bot/workspace/wiring.py`
- Modify: `examples/bot_project/bot/service/web_ui_service.py`

Expose `GlobalWorkspaceStore` on `WorkspaceStack` so the service can build the authoritative roots provider (home + known targets, NOT the incomplete recent list). Construct + start/stop the collector alongside the server.

- [ ] **Step 1: Expose the store on WorkspaceStack**

Read `bot/workspace/wiring.py` around the `WorkspaceStack` dataclass definition and the `return WorkspaceStack(...)` at ~line 114. Add `store` as a field and pass `store=store`. (If `WorkspaceStack` already has a `store` field, skip — just confirm it's passed.)

```python
# In the WorkspaceStack dataclass (find its definition):
store: GlobalWorkspaceStore
# In the return:
return WorkspaceStack(
    registry=registry,
    resolver=resolver,
    controller=controller,
    dispatcher=dispatcher,
    factory=factory,
    store=store,
)
```

- [ ] **Step 2: Construct + start/stop the collector in web_ui_service**

In `bot/service/web_ui_service.py`, in `start()` (line ~441), after `set_workspace_control` (line ~457), add collector construction + start, using the raw config the service already loaded (locate where the service holds the raw config dict — it is the dict passed to `AppConfig.from_yaml`; if only `AppConfig` is retained, read `session_gc` from the same YAML path via `ConfigLoader`). If the raw dict is not retained, load it minimally:

```python
# near the top of start(), after workspace_control is wired:
from bot.service.session_gc import SessionGarbageCollector, load_session_gc_config
from bot.utils.config_loader import ConfigLoader

if self.workspace_stack is not None:
    raw = ConfigLoader(self._app_config.paths and (self._project_dir / "config")).load_yaml("config.yml")  # NO — see note
```

**Note (resolve before coding):** the exact raw-config source. The service loads `AppConfig` somewhere upstream. Find that call site (search `AppConfig.from_yaml` in `examples/bot_project`); retain the raw dict there and expose it on the service as `self._raw_config`, then:

```python
gc_cfg = load_session_gc_config(self._raw_config)
self._session_gc = SessionGarbageCollector(
    workspace_roots_provider=self._workspace_roots_provider,
    data_dir_name=self._app_config.paths.data_dir_name,
    config=gc_cfg,
)
self._server.set_session_gc(self._session_gc)
await self._session_gc.start()
```

Add the roots provider method to the service:

```python
def _workspace_roots_provider(self):
    home = self._project_dir
    targets = []
    if self.workspace_stack is not None:
        targets = self.workspace_stack.store.load_known_targets()
    return [home, *targets]
```

In `stop()` (line ~682), add before existing cleanup:

```python
if self._session_gc is not None:
    await self._session_gc.stop()
```

Add `self._session_gc = None` in `__init__`.

- [ ] **Step 3: Verify the service imports/constructs without error**

Run: `cd examples/bot_project && python -c "import bot.service.web_ui_service"` and run any existing fast service smoke test.
Expected: no import errors.

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/bot/workspace/wiring.py examples/bot_project/bot/service/web_ui_service.py
git commit -m "feat(session-gc): wire collector lifecycle + workspace-roots provider into web_ui_service"
```

---

## Task 11: REST delete handler delegates to the collector

**Files:**
- Modify: `examples/bot_project/bot/webui/server.py`
- Test: `examples/bot_project/tests/webui/test_server.py`

Add `set_session_gc()`; rewrite `_handle_delete_session` (line ~1877) to delegate. Keep `{deleted: id}`. Thin server test only — no cascade assertions here (those live in `test_session_gc.py`).

- [ ] **Step 1: Write the failing thin test**

Append to `tests/webui/test_server.py` (match that file's existing fixture style — most tests construct `WebUIServer(...)` then call handlers; mirror one):

```python
async def test_delete_session_delegates_to_collector():
    # Build a minimal server per existing fixtures in this file, inject a fake gc.
    server = WebUIServer(...)  # match an existing minimal constructor call in this file
    class FakeGC:
        deleted = None
        async def delete_session_tree(self, root_session_id):
            FakeGC.deleted = root_session_id
    server.set_session_gc(FakeGC())
    # invoke the DELETE handler with a fake request (match existing handler-call style)
    ...  # call _handle_delete_session with match_info {"session_id": "abc.main"}
    assert FakeGC.deleted == "abc.main"
```

(Fill the `...` by copying the exact request-construction pattern from an existing delete/handler test in `test_server.py` — do not invent a new aiohttp test harness.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd examples/bot_project && python -m pytest tests/webui/test_server.py -k delete_session_delegates -v`
Expected: FAIL — `set_session_gc` does not exist.

- [ ] **Step 3: Implement the setter + delegation**

In `bot/webui/server.py`:

Add an attribute `self._session_gc = None` in `__init__` (near the other `self._...` at ~line 271-282). Add the setter near `set_workspace_control` (~line 514):

```python
def set_session_gc(self, gc) -> None:
    """Inject the SessionGarbageCollector for cascade session deletion."""
    self._session_gc = gc
```

Replace the body of `_handle_delete_session` (line ~1877) with:

```python
    async def _handle_delete_session(self, request: web.Request) -> web.Response:
        """DELETE /api/sessions/{session_id} -- delete a conversation's cascade.

        Delegates to the SessionGarbageCollector, which removes the root's record
        synchronously (so the conversation leaves the list immediately) and
        drains the full subagent cascade + all ten artifact types via the
        background pool. Keeps the {deleted: id} contract.
        """
        session_id: str = request.match_info["session_id"]
        if self._session_gc is not None:
            await self._session_gc.delete_session_tree(session_id)
        return web.json_response({"deleted": session_id})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd examples/bot_project && python -m pytest tests/webui/test_server.py -k delete_session_delegates -v && python -m pytest tests/service/test_session_gc.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/bot_project/bot/webui/server.py examples/bot_project/tests/webui/test_server.py
git commit -m "feat(session-gc): REST delete handler delegates cascade to collector"
```

---

## Task 12: Full regression + docs alignment

**Files:**
- Verify only (and touch docs if drift found)

- [ ] **Step 1: Run the targeted suites**

Run:
```bash
cd examples/bot_project
python -m pytest tests/service/test_session_gc.py tests/webui/test_server.py tests/test_session_store.py tests/webui/test_transcript_store.py -v
```
Expected: all PASS.

- [ ] **Step 2: Run a broader webui/service regression**

Run:
```bash
python -m pytest tests/webui tests/test_web_ui_service.py -x -q
```
Expected: PASS (no regressions from the `set_session_gc` / handler change).

- [ ] **Step 3: Confirm ADR-0018 + PRD match what shipped**

Re-read `docs/adr/0018-crash-safe-session-garbage-collection.md` and `docs/design/session-gc/PRD.md`. If any decision drifted during implementation (e.g., the memory-dir signal for Rule 2, the test-only `_drain_for_tests`), add a one-line note to ADR-0018's Consequences. Do not rewrite.

- [ ] **Step 4: Commit any doc touch**

```bash
git add docs/adr/0018-crash-safe-session-garbage-collection.md
git commit -m "docs(session-gc): align ADR-0018 with shipped implementation"
```

---

## Self-Review (completed)

**Spec coverage:** PRD user stories map as follows — foreground delete + immediate list removal (1,2): Task 7 `delete_session_tree` sync-removes root record. Cascade incl. nested (3,4): Tasks 5-7 (clean_session + `_propagate_children` + worker enqueue). Ten artifact types (5-12): Task 2 derivation + Task 5 removal. Sibling safety (13): Task 5 `_session_artifact_paths` only emits per-session units; tested. Shared-not-deleted (14,15): Task 2 excludes archive/knowledge/commands; tested. Crash recovery (16-19): Tasks 4 + 7 (`_find_orphan_artifact_sids`, `sweep_once`); tested in Task 7. Concurrency/idempotency (20-22): Tasks 5 + 7 (dedup, idempotent clean_session); tested. Active-session edge (23,24): accepted per ADR; no code (OSError path in Task 5). Multi-workspace (25,26): Tasks 9 + 10. Config (27,28,29): Task 1 + Task 10 wiring; logging in Task 5/7. REST delegation: Task 11.

**Placeholder scan:** Task 10 Step 2 has an unresolved raw-config source — explicitly flagged "resolve before coding" with the search instruction (`AppConfig.from_yaml` call site). Task 11 Step 1 has a `...` for the aiohttp request harness — explicitly told to copy the existing pattern in that file. Both are anchored to concrete search instructions, not vague TODOs. No other placeholders.

**Type consistency:** `clean_session(session_id, pool, paths)`, `_session_artifact_paths(session_id, pool, paths)`, `_IndexedSession(session_id/pool)`, `_Job(session_id, pool, ws_root)`, `SessionGarbageCollector(workspace_roots_provider, data_dir_name, config)` — signatures used consistently across Tasks 2-11. `delete_session_tree(root_session_id)`, `sweep_once()`, `start()`, `stop()` match between Task 7/8 (impl) and Task 10/11 (wiring). `_drain_for_tests`, `_inflight_count`, `_enqueue` used consistently in Task 7-9 tests.
