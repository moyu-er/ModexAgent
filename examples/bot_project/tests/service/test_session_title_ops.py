"""PA-01 tests — shared title write ops over the REAL SessionRegistry/store.

Covers the DESIGN §2.6 contract:
- trim, non-empty, single-line, <=80 chars validation (400 semantics)
- incremental metadata={"title": value} via the SAME runtime registry
- other metadata keys / parent / timestamps preserved
- touch/register after a title write keeps the title (V08)
- missing session -> not found (no implicit register create)
- same-value save is a no-op success (V05)
- FILE and SQLite backends agree
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bot.service.session_title import (
    SessionTitleOps,
    TitleValidationError,
)

from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.persistence.session_registry import InMemorySessionRegistry


def _file_store(tmp_path: Path) -> LocalFileSessionStore:
    return LocalFileSessionStore(tmp_path / "session_index")


async def _sqlite_registry(
    tmp_path: Path,
) -> tuple[InMemorySessionRegistry, WorkspacePersistenceManager]:
    from modex_agent.persistence.adapters.session_store import SqliteSessionStore

    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    store = SqliteSessionStore(manager.connection)
    return InMemorySessionRegistry(store=store), manager


async def _seed(registry: InMemorySessionRegistry, session_id: str = "abc123.main") -> None:
    await registry.register(
        SessionInfo(
            session_id=session_id,
            agent_name="main",
            parent_session_id=None,
            created_at=1000,
            updated_at=2000,
            metadata={"pool": "default", "channel": "websocket"},
        )
    )


async def _make_ops(
    tmp_path: Path, backend: str
) -> tuple[SessionTitleOps, InMemorySessionRegistry, WorkspacePersistenceManager | None]:
    if backend == "file":
        store = _file_store(tmp_path)
        registry = InMemorySessionRegistry(store=store)
        await registry.load_all()
        return SessionTitleOps(registry=registry), registry, None
    registry, manager = await _sqlite_registry(tmp_path)
    return SessionTitleOps(registry=registry), registry, manager


@pytest.mark.parametrize("backend", ["file", "sqlite"])
@pytest.mark.asyncio
async def test_set_title_merges_and_preserves_other_metadata(tmp_path: Path, backend: str) -> None:
    ops, registry, manager = await _make_ops(tmp_path, backend)
    await _seed(registry)

    await ops.set_title("abc123.main", "杭州旅行规划")

    session = await registry.get("abc123.main")
    assert session is not None
    assert session.metadata["title"] == "杭州旅行规划"
    assert session.metadata["pool"] == "default"
    assert session.metadata["channel"] == "websocket"
    assert session.parent_session_id is None
    assert session.created_at == 1000
    # a NEW session index read (fresh store over the same disk) sees the title
    if backend == "file":
        fresh = InMemorySessionRegistry(store=_file_store(tmp_path))
        await fresh.load_all()
        persisted = await fresh.get("abc123.main")
        assert persisted is not None
        assert persisted.metadata["title"] == "杭州旅行规划"
    else:
        assert manager is not None
        await manager.close()


@pytest.mark.parametrize("backend", ["file", "sqlite"])
@pytest.mark.asyncio
async def test_touch_and_register_after_title_keep_it(tmp_path: Path, backend: str) -> None:
    ops, registry, manager = await _make_ops(tmp_path, backend)
    await _seed(registry)
    await ops.set_title("abc123.main", "工作会话")

    # runtime touch (updated_at bump) must not drop the title
    await registry.touch("abc123.main")
    # a plain incremental register merging new metadata must not drop it either
    await registry.register(
        SessionInfo(session_id="abc123.main", agent_name="main", metadata={"foo": "bar"})
    )

    session = await registry.get("abc123.main")
    assert session is not None
    assert session.metadata["title"] == "工作会话"
    assert session.metadata["foo"] == "bar"
    if backend == "sqlite":
        assert manager is not None
        await manager.close()


@pytest.mark.asyncio
async def test_set_title_missing_session_raises_not_found(tmp_path: Path) -> None:
    ops, registry, _ = await _make_ops(tmp_path, "file")
    with pytest.raises(LookupError):
        await ops.set_title("missing.main", "不存在")
    # must NOT implicitly create the record
    assert await registry.get("missing.main") is None


@pytest.mark.asyncio
async def test_set_title_same_value_is_noop_success(tmp_path: Path) -> None:
    ops, registry, _ = await _make_ops(tmp_path, "file")
    await _seed(registry)
    await ops.set_title("abc123.main", "同一标题")
    first = await registry.get("abc123.main")
    await ops.set_title("abc123.main", "同一标题")
    second = await registry.get("abc123.main")
    assert first is not None and second is not None
    assert first.updated_at == second.updated_at
    assert second.metadata["title"] == "同一标题"


@pytest.mark.asyncio
async def test_validation_rules(tmp_path: Path) -> None:
    ops, registry, _ = await _make_ops(tmp_path, "file")
    await _seed(registry)
    with pytest.raises(TitleValidationError):
        await ops.set_title("abc123.main", "   ")
    with pytest.raises(TitleValidationError):
        await ops.set_title("abc123.main", "line1\nline2")
    with pytest.raises(TitleValidationError):
        await ops.set_title("abc123.main", "x" * 81)
    # valid: trims to 80
    await ops.set_title("abc123.main", "  " + "y" * 80 + "  ")


@pytest.mark.asyncio
async def test_read_title_and_display_value(tmp_path: Path) -> None:
    ops, registry, _ = await _make_ops(tmp_path, "file")
    await _seed(registry)
    assert await ops.read_title("abc123.main") is None
    await ops.set_title("abc123.main", "  标题  ")
    assert await ops.read_title("abc123.main") == "标题"
    # whitespace-only stored title reads as absent
    await registry.register(SessionInfo(session_id="abc123.main", agent_name="main", metadata={"title": "   "}))
    assert await ops.read_title("abc123.main") is None
