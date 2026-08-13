"""Regression: WebUI todos and approvals endpoints must use the SQLite backend.

Before the fix, ``_handle_get_todos`` hardcoded ``JsonFileTodoStore`` (file)
and ``_handle_get_approvals`` hardcoded ``JsonFileTurnStateStore`` (file),
while the agent writes to ``SqliteTodoStore`` and ``SqliteTurnStateStore``.
In SQLite mode (the bot default), both endpoints read from stores that were
never written to, so the WebUI showed empty todos and empty approvals.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.webui.server import RuntimeStores, WebUIServer


async def _async_return(value: RuntimeStores) -> RuntimeStores:
    """Wrap a RuntimeStores in a coroutine for use as an async store resolver."""
    return value


@pytest.fixture()
def sqlite_db(tmp_path: Path) -> Iterator[Path]:
    """Create a migrated workspace SQLite DB."""
    from modex_agent.persistence import ConnectionManager, DatabaseKind

    db_path = tmp_path / ".modex" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    mgr = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
    asyncio.run(mgr.open())
    asyncio.run(mgr.close())
    yield db_path


class TestSqliteTodosEndpoint:
    """GET /api/sessions/{id}/todos must read from SqliteTodoStore, not JsonFileTodoStore."""

    @pytest.mark.asyncio
    async def test_sqlite_todos_returned(self, sqlite_db: Path, tmp_path: Path) -> None:
        session_id = "abc123.default"

        # Seed the SQLite todos table directly.
        scope = json.dumps({"pool": "default"}, ensure_ascii=False)
        items = json.dumps(
            [
                {"content": "task A", "status": "in_progress"},
                {"content": "task B", "status": "pending"},
            ],
            ensure_ascii=False,
        )
        conn = sqlite3.connect(str(sqlite_db))
        conn.execute(
            "INSERT INTO todos (session_id, scope_key, items_json, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, scope, items, 1000),
        )
        conn.commit()
        conn.close()

        # Build a SqliteTodoStore for the resolver.
        from modex_agent.persistence import ConnectionManager, DatabaseKind
        from modex_agent.persistence.adapters.todo_store import SqliteTodoStore
        from bot.scope import BotRecordScope

        mgr = ConnectionManager(sqlite_db, DatabaseKind.WORKSPACE)
        await mgr.open()
        todo_store = SqliteTodoStore(mgr, BotRecordScope(pool="default"))

        server = WebUIServer(
            input_adapter=MagicMock(),
            transcript_store=MagicMock(),
            home_sessions_dir=tmp_path / ".modex" / "sessions",
        )
        server.set_data_dir_name(".modex")
        server.set_store_resolver(
            lambda ws_root, pool: _async_return(RuntimeStores(todo_store=todo_store))
        )

        async with TestClient(TestServer(server.app)) as client:
            resp = await client.get(f"/api/sessions/{session_id}/todos")
            assert resp.status == 200
            data = await resp.json()
            assert len(data) == 2
            assert data[0]["content"] == "task A"
            assert data[0]["status"] == "in_progress"

        await mgr.close()


class TestSqliteApprovalsEndpoint:
    """GET /api/sessions/{id}/approvals must read from SqliteTurnStateStore, not JsonFileTurnStateStore."""

    @pytest.mark.asyncio
    async def test_sqlite_approvals_returned(self, sqlite_db: Path, tmp_path: Path) -> None:
        session_id = "abc456.default"

        from modex_agent.agents.react.state import ReActRuntimeStateCodec
        from modex_agent.core.message import ChatMessage
        from modex_agent.core.session_id import SessionInfo
        from modex_agent.core.types import MessageRole
        from modex_agent.persistence import ConnectionManager, DatabaseKind
        from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore
        from modex_agent.runtime.codec import RuntimeStateCodecRegistry
        from modex_agent.runtime.enums import (
            AgentKind,
            MessageDeltaSource,
            SnapshotReason,
            TurnPhase,
        )
        from modex_agent.runtime.models import (
            MessageDelta,
            ResumePoint,
            TurnIdentity,
            TurnSnapshot,
        )

        mgr = ConnectionManager(sqlite_db, DatabaseKind.WORKSPACE)
        await mgr.open()
        codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
        turn_store = SqliteTurnStateStore(mgr, codec_registry)

        session = SessionInfo.from_str(session_id)
        snapshot = TurnSnapshot(
            identity=TurnIdentity(
                session=session,
                agent_id="default",
                turn_id="turn-1",
            ),
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.SUSPENDED,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
            resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.SUSPENDED),
            message_delta=[
                MessageDelta(
                    message=ChatMessage(role=MessageRole.ASSISTANT, content=""),
                    source=MessageDeltaSource.ASSISTANT,
                )
            ],
            created_at=1000.0,
            schema_version=1,
            state_payload={
                "approval": {
                    "approval_id": "appr-1",
                    "turn_id": "turn-1",
                    "subject_type": "tool_call",
                    "subject_ids": ["tc-1"],
                    "requests": [
                        {
                            "request_id": "req-1",
                            "approval_id": "appr-1",
                            "tool_call_id": "tc-1",
                            "tool_name": "bash",
                            "arguments": {"values": {"command": "ls"}},
                            "tier": "normal",
                            "iteration": 1,
                            "created_at": 1000.0,
                        }
                    ],
                    "decisions": {},
                    "status": "pending",
                    "deny_reason": None,
                }
            },
        )
        await turn_store.save_turn(snapshot)

        server = WebUIServer(
            input_adapter=MagicMock(),
            transcript_store=MagicMock(),
            home_sessions_dir=tmp_path / ".modex" / "sessions",
        )
        server.set_data_dir_name(".modex")
        server.set_store_resolver(
            lambda ws_root, pool: _async_return(RuntimeStores(turn_store=turn_store))
        )

        async with TestClient(TestServer(server.app)) as client:
            resp = await client.get(f"/api/sessions/{session_id}/approvals")
            assert resp.status == 200
            data = await resp.json()
            assert len(data) == 1
            assert data[0]["tool_name"] == "bash"
            assert data[0]["tool_call_id"] == "tc-1"

        await mgr.close()
