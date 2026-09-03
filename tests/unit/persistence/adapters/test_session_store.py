"""Tests for :class:`SqliteSessionStore`.

Covers session CRUD, parent-child graph queries, prefix-based listing, and
generated-column derivation from the ``scope_key`` JSON.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters import SqliteSessionStore
from modex_agent.utils.time import now_ms


async def _open_store(tmp_path: Path) -> tuple[ConnectionManager, SqliteSessionStore]:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    return manager, SqliteSessionStore(manager)


# ── CRUD ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_and_get_roundtrips_session(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        session = SessionInfo(
            session_id="conv123.main",
            agent_name="main",
            metadata={"pool": "default", "channel": "webui"},
        )
        await store.save(session)

        result = await store.get("conv123.main")
        assert result is not None
        assert result.session_id == "conv123.main"
        assert result.agent_name == "main"
        assert result.metadata.get("channel") == "webui"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_get_nonexistent_returns_none(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        assert await store.get("no.such.session") is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_save_upsert_updates_existing(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        session = SessionInfo(
            session_id="conv.upsert",
            agent_name="main",
            metadata={"pool": "default", "version": 1},
        )
        await store.save(session)

        updated = session.model_copy(update={"metadata": {"pool": "default", "version": 2}})
        await store.save(updated)

        result = await store.get("conv.upsert")
        assert result is not None
        assert result.metadata.get("version") == 2
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_save_preserves_created_at_on_update(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        session = SessionInfo(
            session_id="conv.preserve",
            agent_name="main",
            created_at=now_ms(),
            metadata={"pool": "default"},
        )
        await store.save(session)

        original = await store.get("conv.preserve")
        assert original is not None
        assert original.created_at is not None

        # Save again with a different updated_at; created_at must stay.
        updated = session.model_copy(update={"metadata": {"pool": "default", "v": 2}})
        await store.save(updated)

        result = await store.get("conv.preserve")
        assert result is not None
        assert result.created_at == original.created_at
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_delete_removes_session(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        session = SessionInfo(
            session_id="conv.del",
            agent_name="main",
            metadata={"pool": "default"},
        )
        await store.save(session)
        await store.delete("conv.del")
        assert await store.get("conv.del") is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_delete_nonexistent_is_noop(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        await store.delete("never.existed")  # must not raise
    finally:
        await manager.close()


# ── list_sessions ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sessions_returns_all(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        for sid in ("aaa.main", "bbb.main", "ccc.sub"):
            await store.save(
                SessionInfo(
                    session_id=sid, agent_name=sid.split(".")[1], metadata={"pool": "default"}
                )
            )
        sessions = await store.list_sessions()
        ids = [s.session_id for s in sessions]
        assert ids == ["aaa.main", "bbb.main", "ccc.sub"]
    finally:
        await manager.close()


# ── list_by_prefix ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_by_prefix_returns_matching(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        # Two sessions share prefix "conv1", one has "conv2".
        await store.save(
            SessionInfo(session_id="conv1.main", agent_name="main", metadata={"pool": "default"})
        )
        await store.save(
            SessionInfo(
                session_id="conv1.coder",
                agent_name="coder",
                parent_session_id="conv1.main",
                metadata={"pool": "default"},
            )
        )
        await store.save(
            SessionInfo(session_id="conv2.main", agent_name="main", metadata={"pool": "default"})
        )

        result = await store.list_by_prefix("conv1")
        ids = [s.session_id for s in result]
        assert ids == ["conv1.coder", "conv1.main"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_list_by_prefix_no_matches_returns_empty(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        result = await store.list_by_prefix("nonexistent")
        assert result == []
    finally:
        await manager.close()


# ── get_children ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_children_returns_children(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        parent = SessionInfo(
            session_id="parent.main", agent_name="main", metadata={"pool": "default"}
        )
        child1 = SessionInfo(
            session_id="parent.coder",
            agent_name="coder",
            parent_session_id="parent.main",
            metadata={"pool": "default"},
        )
        child2 = SessionInfo(
            session_id="parent.reviewer",
            agent_name="reviewer",
            parent_session_id="parent.main",
            metadata={"pool": "default"},
        )
        other = SessionInfo(
            session_id="other.main", agent_name="main", metadata={"pool": "default"}
        )
        for s in (parent, child1, child2, other):
            await store.save(s)

        children = await store.get_children("parent.main")
        ids = [c.session_id for c in children]
        assert ids == ["parent.coder", "parent.reviewer"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_get_children_no_children_returns_empty(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        await store.save(
            SessionInfo(session_id="leaf.main", agent_name="main", metadata={"pool": "default"})
        )
        children = await store.get_children("leaf.main")
        assert children == []
    finally:
        await manager.close()


# ── generated columns from scope_key ───────────────────────────────────────


@pytest.mark.asyncio
async def test_parent_session_id_in_scope(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        await store.save(
            SessionInfo(
                session_id="c.child",
                agent_name="child",
                parent_session_id="c.parent",
                metadata={"pool": "default"},
            )
        )
        row = await manager.query_one(
            "SELECT parent_session_id FROM sessions WHERE session_id = ?",
            ("c.child",),
        )
        assert row is not None
        assert row[0] == "c.parent"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_timestamp_roundtrip(tmp_path: Path) -> None:
    manager, store = await _open_store(tmp_path)
    try:
        ts = now_ms()
        session = SessionInfo(
            session_id="ts.main",
            agent_name="main",
            created_at=ts,
            updated_at=ts,
            metadata={"pool": "default"},
        )
        await store.save(session)
        result = await store.get("ts.main")
        assert result is not None
        assert result.created_at is not None
        assert result.updated_at is not None
        # DB stores seconds precision, so allow ±1s drift.
        assert abs(result.created_at - ts) <= 1000
        assert abs(result.updated_at - ts) <= 1000
    finally:
        await manager.close()
