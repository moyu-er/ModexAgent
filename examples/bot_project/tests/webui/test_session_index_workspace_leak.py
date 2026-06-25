"""Regression: session-index records must NEVER leak to the HOME workspace.

The symptom (reported 2026-06-21): switching workspace shows sessions from
OTHER workspaces. The user's directive: "绝不应该有任何共享/全局会话目录"
(there must NEVER be any shared/global session directory).

Root cause hypothesis: ``WorkspacePoolSessionStore`` is constructed with
``base_dir=home_session_index`` (production wiring in
``web_ui_service.py:142``). Its ``_root_for(index_dir=None)`` falls back to
``self._root`` (= home session_index) whenever:

  1. the caller passes no ``index_dir`` (e.g. ``InMemorySessionRegistry``
     calls ``store.save(session)`` / ``store.list_sessions()`` with no
     ``index_dir``), AND
  2. the workspace-root contextvar is not bound.

Even inside a dispatch turn (contextvar bound), the framework's
``AgentCommunicationService`` calls ``session_registry.register(child)``
which calls ``store.save(session)`` with NO ``index_dir``. If that call
ever runs outside the bound contextvar — or if the registry's shared
``_cache`` is seeded from a ``load_all()`` that reads home — subagent
sessions created in a non-home workspace get indexed into HOME's
``session_index/``, and then leak into every workspace's listing that
falls back to home.

These tests drive the real ``WorkspacePoolSessionStore`` the way
``web_ui_service`` wires it (``base_dir = home session_index``) and assert
that a session registered while a non-home workspace is active lands in
THAT workspace's index, never home's.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from bot.service.session_store import WorkspacePoolSessionStore

from modex_agent.core.session_id import SessionInfo, now_ms
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.workspace.runtime import bind_workspace_root

_DATA_DIR_NAME = ".modex"


def _ws_index_dir(ws_root: Path) -> Path:
    return ws_root / _DATA_DIR_NAME / "session_index"


@pytest.mark.asyncio
async def test_register_subagent_under_non_home_workspace_does_not_leak_to_home() -> None:
    """A subagent SessionInfo registered while workspace-root is bound to a
    non-home workspace must be persisted into THAT workspace's session_index,
    not the home session_index.

    Reproduces the framework call chain:
      AgentCommunicationService._create_dynamic_subagent
        -> session_registry.register(child_session)
        -> store.save(session)   # NO index_dir passed
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "ws_a"
        ws_a.mkdir()

        # Production wiring: base_dir = HOME session_index.
        store = WorkspacePoolSessionStore(
            base_dir=_ws_index_dir(home),
            pool_resolver=lambda s: "main",
            data_dir_name=_DATA_DIR_NAME,
        )

        child_sid = "convA.main.query-12306.inv1"
        child = SessionInfo(
            session_id=child_sid,
            agent_name="query-12306",
            parent_session_id="convA.main",
            created_at=now_ms(),
            updated_at=now_ms(),
        )

        # Simulate the dispatch turn: workspace root bound to ws_a.
        with bind_workspace_root(ws_a):
            await store.save(child)

        # The record MUST exist under ws_a's session_index.
        ws_a_records = list(_ws_index_dir(ws_a).rglob("*.json"))
        assert any(child_sid in f.name for f in ws_a_records), (
            f"subagent session {child_sid} must be indexed under ws_a; "
            f"ws_a records={[f.name for f in ws_a_records]}"
        )

        # The record MUST NOT exist under home's session_index.
        home_records = list(_ws_index_dir(home).rglob("*.json"))
        assert not any(child_sid in f.name for f in home_records), (
            f"LEAK: subagent session {child_sid} leaked into HOME session_index; "
            f"home records={[f.name for f in home_records]}"
        )


@pytest.mark.asyncio
async def test_in_memory_registry_with_per_workspace_store_routes_to_workspace() -> None:
    """Regression: wiring must pass a per-workspace registry/store to pools.

    Production wiring (``wiring._build_resources``) creates an
    ``InMemorySessionRegistry`` backed by the workspace's
    ``WorkspacePoolSessionStore`` and passes it to ``create_pool``. A direct
    register through that registry must write into the workspace's
    ``session_index/<pool>/`` — not the home session_index.
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "ws_a"
        ws_a.mkdir()

        ws_store = WorkspacePoolSessionStore(
            base_dir=_ws_index_dir(ws_a),
            pool_resolver=lambda s: "coding" if "reviewer" in s.session_id else "main",
            data_dir_name=_DATA_DIR_NAME,
        )
        ws_registry = InMemorySessionRegistry(store=ws_store)

        child_sid = "convA.coding.reviewer.ee11"
        child = SessionInfo(
            session_id=child_sid,
            agent_name="reviewer",
            parent_session_id="convA.coding",
            created_at=now_ms(),
            updated_at=now_ms(),
        )

        with bind_workspace_root(ws_a):
            await ws_registry.register(child)

        expected = _ws_index_dir(ws_a) / "coding" / f"{child_sid}.json"
        assert expected.exists(), (
            f"subagent session {child_sid} must be indexed under ws_a/coding, "
            f"not {list(_ws_index_dir(ws_a).rglob('*.json'))}"
        )

        home_records = list(_ws_index_dir(home).rglob("*.json"))
        assert not any(child_sid in f.name for f in home_records), (
            f"LEAK: subagent session {child_sid} leaked into HOME session_index"
        )


@pytest.mark.asyncio
async def test_list_sessions_without_index_dir_does_not_return_other_workspace_sessions() -> None:
    """``list_sessions()`` with no ``index_dir`` must NOT surface sessions
    that belong to another workspace, even if they were correctly written to
    that workspace's index.

    The ``InMemorySessionRegistry.load_all()`` path calls
    ``store.list_sessions()`` with no ``index_dir`` at startup. If that
    returns sessions from a non-home workspace, the registry's shared cache
    is polluted and every workspace sees every session.
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "ws_a"
        ws_a.mkdir()

        store = WorkspacePoolSessionStore(
            base_dir=_ws_index_dir(home),
            pool_resolver=lambda s: "main",
            data_dir_name=_DATA_DIR_NAME,
        )

        sid_a = "convA.main"
        session_a = SessionInfo(
            session_id=sid_a,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        )

        # Write ws_a's session into ws_a's index (correct, scoped write).
        with bind_workspace_root(ws_a):
            await store.save(session_a)

        # list_sessions() with NO index_dir (what load_all does at startup).
        # It must NOT return ws_a's session — home has no such session.
        loaded = await store.list_sessions()
        loaded_ids = {s.session_id for s in loaded}
        assert sid_a not in loaded_ids, (
            f"LEAK: list_sessions() with no index_dir returned ws_a's session "
            f"{sid_a}; loaded_ids={loaded_ids}. The registry's load_all() "
            f"would pollute its cache with cross-workspace sessions."
        )


@pytest.mark.asyncio
async def test_get_session_without_index_dir_does_not_fall_back_to_home_for_non_home_session() -> None:
    """``get(session_id)`` with no ``index_dir`` must NOT find a session that
    lives in a non-home workspace by scanning home's index.

    ``_path_for`` globs under the resolved root. When ``index_dir=None`` and
    the contextvar is unbound, it globs HOME. A session written to ws_a's
    index should not be discoverable from home.
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "ws_a"
        ws_a.mkdir()

        store = WorkspacePoolSessionStore(
            base_dir=_ws_index_dir(home),
            pool_resolver=lambda s: "main",
            data_dir_name=_DATA_DIR_NAME,
        )

        sid_a = "convA.main"
        session_a = SessionInfo(
            session_id=sid_a,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        )

        # Write ws_a's session into ws_a's index.
        with bind_workspace_root(ws_a):
            await store.save(session_a)

        # get() with NO index_dir, NO bound contextvar → must not find it.
        result = await store.get(sid_a)
        assert result is None, (
            f"LEAK: get({sid_a!r}) with no index_dir found a ws_a session by "
            f"scanning home; result={result}. Cross-workspace discovery."
        )
