from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from bot.persistence.migration import BotWorkspaceMigrationRunner
from bot.service.session_gc import (
    SessionCleanerOperations,
    SessionGarbageCollector,
    SessionGcConfig,
)
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import (
    AssistantTextEvent,
    AssistantTurnEvent,
    ServerEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from bot.webui.sqlite_transcript_store import SqliteTranscriptStore
from bot.webui.transcript_store import ResilientTranscriptStore

from modex_agent.core.cleanup import SessionCleanupResult
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.session_store import SqliteSessionStore
from modex_agent.workspace.paths import WorkspacePaths


@pytest.fixture
async def sqlite_store(
    tmp_path: Path,
) -> AsyncIterator[tuple[SqliteTranscriptStore, ConnectionManager]]:
    connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await connection.open()
    await BotWorkspaceMigrationRunner(connection).run_pending()
    yield SqliteTranscriptStore(connection), connection
    await connection.close()


def _message(
    session_id: str,
    content: str,
    *,
    timestamp: int = 100,
    agent_name: str = "main",
) -> UserMessageEvent:
    return UserMessageEvent(
        session_id=session_id,
        agent_name=agent_name,
        content=content,
        timestamp=timestamp,
    )


def _contents(events: Sequence[ServerEvent]) -> list[object]:
    return [event.to_dict().get("content") for event in events]


async def test_crud_isolated_by_full_session_and_exact_prefix(
    sqlite_store: tuple[SqliteTranscriptStore, ConnectionManager],
) -> None:
    store, _ = sqlite_store
    await store.append("chat.main", _message("chat.main", "main"), pool="main")
    await store.append(
        "chat.reviewer.a1",
        _message("chat.reviewer.a1", "review", agent_name="reviewer"),
        pool="coder",
    )
    await store.append(
        "chatty.main", _message("chatty.main", "other"), pool="main"
    )

    assert _contents(await store.load("chat.main")) == ["main"]
    assert await store.list_sessions_by_prefix("chat") == {
        "chat.main",
        "chat.reviewer.a1",
    }
    assert _contents(await store.load_sessions_by_prefix("chat")) == ["main", "review"]

    await store.delete_session("chat.main")
    assert await store.load("chat.main") == []
    assert len(await store.load("chat.reviewer.a1")) == 1

    await store.delete_sessions_by_prefix("chat")
    assert await store.list_sessions_by_prefix("chat") == set()
    assert await store.list_sessions() == {"chatty.main"}


async def test_pool_filter_and_equal_timestamp_order_are_stable(
    sqlite_store: tuple[SqliteTranscriptStore, ConnectionManager],
) -> None:
    store, _ = sqlite_store
    await store.append(
        "conv.main", _message("conv.main", "first", timestamp=500), pool="main"
    )
    await store.append(
        "conv.worker.a1",
        _message("conv.worker.a1", "second", timestamp=500, agent_name="worker"),
        pool="coder",
    )
    await store.append(
        "conv.main", _message("conv.main", "third", timestamp=500), pool="main"
    )

    assert _contents(await store.load_sessions_by_prefix("conv")) == [
        "first",
        "second",
        "third",
    ]
    assert _contents(await store.load_sessions_by_prefix("conv", pool="main")) == [
        "first",
        "third",
    ]


async def test_structured_events_and_materialization_round_trip(
    sqlite_store: tuple[SqliteTranscriptStore, ConnectionManager],
) -> None:
    store, _ = sqlite_store
    session_id = "conv.main"
    await store.append(
        session_id,
        AssistantTextEvent(
            session_id=session_id,
            agent_name="main",
            turn_id="turn-1",
            text="你好",
            timestamp=100,
        ),
        pool="main",
    )
    await store.append(
        session_id,
        ToolCallEvent(
            session_id=session_id,
            agent_name="main",
            turn_id="turn-1",
            call_id="call-1",
            tool_name="read_file",
            args={"path": "README.md"},
            timestamp=200,
        ),
        pool="main",
    )
    await store.append(
        session_id,
        ToolResultEvent(
            session_id=session_id,
            agent_name="main",
            turn_id="turn-1",
            call_id="call-1",
            tool_name="read_file",
            result="内容",
            timestamp=300,
        ),
        pool="main",
    )
    await store.append(
        session_id,
        AssistantTurnEvent(
            session_id=session_id,
            agent_name="main",
            turn_id="turn-1",
            attachments=[{"id": "file-1", "name": "报告.txt"}],
            timestamp=400,
        ),
        pool="main",
    )

    turns = await store.load_materialized_by_prefix("conv")
    assert turns[0].blocks == [
        {"kind": "text", "text": "你好"},
        {
            "kind": "tool",
            "tool": "read_file",
            "args": {"path": "README.md"},
            "result": "内容",
        },
    ]
    assert turns[0].attachments == [{"id": "file-1", "name": "报告.txt"}]
    assert await store.last_updated(session_id) == 400


async def test_concurrent_append_does_not_drop_events(
    sqlite_store: tuple[SqliteTranscriptStore, ConnectionManager],
) -> None:
    store, _ = sqlite_store
    await asyncio.gather(
        *(
            store.append(
                "conv.main",
                _message("conv.main", f"message-{index}", timestamp=index),
                pool="main",
            )
            for index in range(100)
        )
    )

    events = await store.load("conv.main")
    assert len(events) == 100
    assert set(_contents(events)) == {
        f"message-{index}" for index in range(100)
    }


async def test_hot_queries_use_covering_identity_indexes(
    sqlite_store: tuple[SqliteTranscriptStore, ConnectionManager],
) -> None:
    store, connection = sqlite_store
    await store.append(
        "conv.main", _message("conv.main", "hello"), pool="main"
    )

    session_rows = await connection.query_all(
        "EXPLAIN QUERY PLAN SELECT payload_json FROM bot_webui_transcript_events "
        "WHERE session_id = ? ORDER BY event_id",
        ("conv.main",),
    )
    prefix_rows = await connection.query_all(
        "EXPLAIN QUERY PLAN SELECT payload_json FROM bot_webui_transcript_events "
        "WHERE session_prefix = ? ORDER BY timestamp_ms, event_id",
        ("conv",),
    )
    pool_rows = await connection.query_all(
        "EXPLAIN QUERY PLAN SELECT payload_json FROM bot_webui_transcript_events "
        "WHERE pool_name = ? AND session_prefix = ? "
        "ORDER BY timestamp_ms, event_id",
        ("main", "conv"),
    )
    session_plan = " ".join(str(row[3]) for row in session_rows)
    prefix_plan = " ".join(str(row[3]) for row in prefix_rows)
    pool_plan = " ".join(str(row[3]) for row in pool_rows)

    assert "idx_bot_transcript_session_order" in session_plan
    assert "idx_bot_transcript_prefix_order" in prefix_plan
    assert "idx_bot_transcript_pool_prefix_order" in pool_plan


async def test_new_workspace_access_materializes_complete_database(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "new-workspace"
    workspace.mkdir()
    sessions_dir = workspace / ".modex" / "sessions"
    connection = ConnectionManager(
        workspace / ".modex" / "state.db",
        DatabaseKind.WORKSPACE,
    )
    resolver_calls = 0

    async def resolve(new_sessions_dir: Path) -> SqliteTranscriptStore:
        nonlocal resolver_calls
        resolver_calls += 1
        assert new_sessions_dir == sessions_dir
        await connection.open()
        await BotWorkspaceMigrationRunner(connection).run_pending()
        return SqliteTranscriptStore(connection)

    store = WorkspaceScopedTranscriptStore(
        data_dir_name=".modex",
    )
    store.set_store_resolver(resolve)

    await store.append(
        "conv.main",
        _message("conv.main", "created"),
        sessions_dir=sessions_dir,
    )

    tables = {
        str(row[0])
        for row in await connection.query_all(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "sessions" in tables
    assert "bot_webui_transcript_events" in tables
    assert "bot_schema_migrations" in tables
    assert resolver_calls == 1
    assert _contents(
        await store.load("conv.main", sessions_dir=sessions_dir)
    ) == ["created"]
    await connection.close()


async def test_session_gc_deletes_sqlite_transcript_rows(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions_dir = workspace / ".modex" / "sessions"
    connection = ConnectionManager(
        workspace / ".modex" / "state.db",
        DatabaseKind.WORKSPACE,
    )

    async def resolve(_: Path) -> ResilientTranscriptStore:
        await connection.open()
        await BotWorkspaceMigrationRunner(connection).run_pending()
        return ResilientTranscriptStore(SqliteTranscriptStore(connection))

    store = WorkspaceScopedTranscriptStore(
        data_dir_name=".modex",
    )
    store.set_store_resolver(resolve)
    await store.append(
        "conv.main",
        _message("conv.main", "delete me"),
        sessions_dir=sessions_dir,
    )
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [workspace],
        data_dir_name=".modex",
        config=SessionGcConfig(enabled=False),
        transcript_store=store,
    )

    await gc.delete_session_tree("conv.main", ws_root=workspace, pool="main")
    await gc._drain_for_tests()

    assert await store.load("conv.main", sessions_dir=sessions_dir) == []
    await connection.close()


async def test_workspace_adapter_resolution_is_deduplicated_and_cancel_safe(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "workspace" / ".modex" / "sessions"
    connection = ConnectionManager(
        tmp_path / "workspace" / ".modex" / "state.db",
        DatabaseKind.WORKSPACE,
    )
    release_resolver = asyncio.Event()
    resolver_started = asyncio.Event()
    resolver_calls = 0

    async def resolve(_: Path) -> SqliteTranscriptStore:
        nonlocal resolver_calls
        resolver_calls += 1
        resolver_started.set()
        await release_resolver.wait()
        await connection.open()
        await BotWorkspaceMigrationRunner(connection).run_pending()
        return SqliteTranscriptStore(connection)

    store = WorkspaceScopedTranscriptStore(
        data_dir_name=".modex",
        store_resolver=resolve,
    )
    cancelled_waiter = asyncio.create_task(
        store.list_sessions(sessions_dir=sessions_dir)
    )
    surviving_waiter = asyncio.create_task(
        store.list_sessions(sessions_dir=sessions_dir)
    )
    await resolver_started.wait()
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    release_resolver.set()

    assert await surviving_waiter == set()
    assert resolver_calls == 1
    await connection.close()


async def test_release_workspace_resolves_a_fresh_adapter(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "workspace" / ".modex" / "sessions"
    connection = ConnectionManager(
        tmp_path / "workspace" / ".modex" / "state.db",
        DatabaseKind.WORKSPACE,
    )
    resolver_calls = 0

    async def resolve(_: Path) -> SqliteTranscriptStore:
        nonlocal resolver_calls
        resolver_calls += 1
        await connection.open()
        await BotWorkspaceMigrationRunner(connection).run_pending()
        return SqliteTranscriptStore(connection)

    store = WorkspaceScopedTranscriptStore(
        data_dir_name=".modex",
        store_resolver=resolve,
    )
    assert await store.list_sessions(sessions_dir=sessions_dir) == set()
    store.release_workspace(sessions_dir)
    assert await store.list_sessions(sessions_dir=sessions_dir) == set()

    assert resolver_calls == 2
    await connection.close()


async def test_release_during_resolution_does_not_cache_stale_adapter(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / ".modex" / "sessions"
    started = asyncio.Event()
    proceed = asyncio.Event()
    resolver_calls = 0
    connections: list[ConnectionManager] = []

    async def resolve(_: Path) -> SqliteTranscriptStore:
        nonlocal resolver_calls
        resolver_calls += 1
        connection = ConnectionManager(
            tmp_path / f"state-{resolver_calls}.db", DatabaseKind.WORKSPACE
        )
        connections.append(connection)
        await connection.open()
        await BotWorkspaceMigrationRunner(connection).run_pending()
        started.set()
        await proceed.wait()
        return SqliteTranscriptStore(connection)

    store = WorkspaceScopedTranscriptStore(".modex", store_resolver=resolve)
    first = asyncio.create_task(store.list_sessions(sessions_dir=sessions_dir))
    await started.wait()
    store.release_workspace(sessions_dir)
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await store.list_sessions(sessions_dir=sessions_dir) == set()
    assert resolver_calls == 2
    for connection in connections:
        await connection.close()


async def test_session_gc_deletes_sqlite_descendant_transcripts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    connection = ConnectionManager(
        workspace / ".modex" / "state.db", DatabaseKind.WORKSPACE
    )
    await connection.open()
    await BotWorkspaceMigrationRunner(connection).run_pending()
    transcript = SqliteTranscriptStore(connection)
    session_store = SqliteSessionStore(connection, pool_resolver=lambda _: "main")
    root = SessionInfo(session_id="conv.main", agent_name="main")
    child = SessionInfo(
        session_id="conv.worker.a1",
        agent_name="worker",
        parent_session_id=root.session_id,
    )
    await session_store.save(root)
    await session_store.save(child)
    await transcript.append(root.session_id, _message(root.session_id, "root"))
    await transcript.append(child.session_id, _message(child.session_id, "child"))

    async def resolve_session_store(_: Path) -> SqliteSessionStore:
        return session_store

    router = WorkspaceScopedTranscriptStore(
        ".modex", store_resolver=lambda _: asyncio.sleep(0, result=transcript)
    )
    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [workspace],
        data_dir_name=".modex",
        config=SessionGcConfig(enabled=False),
        transcript_store=router,
        session_store_resolver=resolve_session_store,
        session_pool_resolver=lambda _: "main",
    )
    await gc.delete_session_tree(root.session_id, ws_root=workspace, pool="main")
    await gc._drain_for_tests()

    assert await transcript.list_sessions_by_prefix("conv") == set()
    await connection.close()


async def test_session_gc_sweep_preserves_live_sqlite_sessions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    connection = ConnectionManager(
        workspace / ".modex" / "state.db", DatabaseKind.WORKSPACE
    )
    await connection.open()
    session_store = SqliteSessionStore(connection, pool_resolver=lambda _: "main")
    await session_store.save(SessionInfo(session_id="live.main", agent_name="main"))

    class RecordingCleaner(SessionCleanerOperations):
        live_ids: frozenset[str] = frozenset()

        async def discover_orphan_scopes(
            self,
            paths: WorkspacePaths,
            *,
            live_session_ids: frozenset[str],
            workspace_id: str,
        ) -> list[RecordScope]:
            del paths, workspace_id
            self.live_ids = live_session_ids
            return []

        async def clean_session_artifacts(
            self, paths: WorkspacePaths, session_id: str, scope: RecordScope
        ) -> SessionCleanupResult:
            del paths, session_id, scope
            return SessionCleanupResult()

    cleaner = RecordingCleaner()

    async def resolve_session_store(_: Path) -> SqliteSessionStore:
        return session_store

    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [workspace],
        data_dir_name=".modex",
        config=SessionGcConfig(enabled=True),
        cleaner_factory=cleaner,
        session_store_resolver=resolve_session_store,
    )
    await gc.sweep_once()

    assert cleaner.live_ids == frozenset({"live.main"})
    await connection.close()
