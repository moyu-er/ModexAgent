from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import anyio
import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.fan_in import FanInInputAdapter
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.input_pipeline.assembly import build_webui_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.persistence.transcript import (
    build_database_transcript_store,
    build_transcript_store_resolver,
)
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import WebUIEventType, _unwrap_envelope
from bot.webui.server import WebUIServer
from bot.webui.transcript_store import TranscriptStore
from bot.workspace.dispatch import WorkspaceMessageDispatcher

from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.types import InputMessage
from modex_agent.persistence.adapters.session_store import SqliteSessionStore
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.registry import WorkspaceRegistry
from modex_agent.workspace.routing import WorkspaceResolver
from modex_agent.workspace.store import GlobalWorkspaceStore
from tests.unit.workspace._stubs import StubFactory, StubResources
from tests.webui._pipeline_fixture import _bot_model_config, _NoSkillRegistry


@pytest.mark.asyncio
async def test_ws_sqlite_send_persists_user_before_enqueue(tmp_path: Path) -> None:
    # Given
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    paths = WorkspacePaths(root=workspace_root / ".modex")
    persistence = WorkspacePersistenceManager(paths.state_db)
    await persistence.open()
    connection = persistence.connection
    session_store = SqliteSessionStore(connection, pool_resolver=lambda _: "main")

    async def resolve_transcript(_: Path) -> TranscriptStore:
        return await build_database_transcript_store(connection)

    transcript_resolver = build_transcript_store_resolver(
        PersistenceBackend.SQLITE,
        resolve_transcript,
    )
    assert transcript_resolver is not None
    transcript_store = WorkspaceScopedTranscriptStore(
        data_dir_name=".modex",
        store_resolver=transcript_resolver,
    )
    transcript_store.set_agent_pool_map({"main": "main"})
    input_adapter = WebSocketInputAdapter()
    server = WebUIServer(
        input_adapter,
        transcript_store,
        static_dist=None,
        home_sessions_dir=paths.sessions_dir,
    )
    server.set_data_dir_name(".modex")
    server.set_workspace_index(transcript_store)
    server.set_session_store(session_store)
    server.set_session_factory(SessionIdFactory())
    server.set_agent_pool_map({"main": "main"})
    pool_store = MagicMock()
    pool_store.get.return_value = "main"
    server.set_input_pipeline(
        build_webui_pipeline(
            skill_registry=_NoSkillRegistry(),
            bot_model_config=_bot_model_config(),
        )
    )
    server.set_input_context(
        BotInputContext(
            default_pool="main",
            pool_session_store=pool_store,
            agent_pool_map={"main": "main"},
            agent_resolver=lambda pool: pool,
            transcript_store=transcript_store,
            enqueue_message=input_adapter.put_input_message,
            command_adapter=input_adapter,
            current_ws_provider=lambda: workspace_root,
        )
    )
    client = TestClient(TestServer(server.app))
    await client.start_server()

    try:
        websocket = await client.ws_connect("/ws")
        session_prefix = "sqlite-regression"
        session_id = f"{session_prefix}.main"
        await websocket.send_json(
            {
                "action": "attach",
                "uuid_prefix": session_prefix,
                "pool": "main",
                "ws": str(workspace_root),
            }
        )
        attached = _unwrap_envelope(await websocket.receive_json(timeout=1))
        assert attached["event"] == WebUIEventType.ATTACHED.value

        # When
        await websocket.send_json(
            {
                "action": "send_message",
                "session_id": session_id,
                "content": "sqlite regression",
                "ws": str(workspace_root),
            }
        )

        # Then
        with anyio.fail_after(1):
            echoed = _unwrap_envelope(await websocket.receive_json())
        assert echoed["event"] == WebUIEventType.USER_MESSAGE.value
        assert await session_store.get(session_id) is not None
        events = await transcript_store.load(session_id, sessions_dir=paths.sessions_dir)
        assert [event.to_dict().get("content") for event in events] == [
            "sqlite regression"
        ]
        with anyio.fail_after(1):
            message = await anext(input_adapter.receive())
        assert message.session.session_id == session_id
        assert message.content == "sqlite regression"
    finally:
        await client.close()
        await persistence.close()


@pytest.mark.asyncio
async def test_ws_sqlite_send_reaches_workspace_dispatcher(tmp_path: Path) -> None:
    # Given
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    paths = WorkspacePaths(root=workspace_root / ".modex")
    persistence = WorkspacePersistenceManager(paths.state_db)
    await persistence.open()
    connection = persistence.connection
    session_store = SqliteSessionStore(connection, pool_resolver=lambda _: "main")

    async def resolve_transcript(_: Path) -> TranscriptStore:
        return await build_database_transcript_store(connection)

    transcript_resolver = build_transcript_store_resolver(
        PersistenceBackend.SQLITE,
        resolve_transcript,
    )
    assert transcript_resolver is not None
    transcript_store = WorkspaceScopedTranscriptStore(
        data_dir_name=".modex",
        store_resolver=transcript_resolver,
    )
    transcript_store.set_agent_pool_map({"main": "main"})
    websocket_input = WebSocketInputAdapter()
    fan_in = FanInInputAdapter()
    fan_in.add_source(websocket_input)
    registry = WorkspaceRegistry(
        home=workspace_root,
        data_dir_name=".modex",
        factory=StubFactory(),
        store=GlobalWorkspaceStore(home=workspace_root, data_dir_name=".modex"),
    )
    resolver = WorkspaceResolver(registry=registry)
    routed: list[tuple[Path, InputMessage]] = []

    async def route_one(resources: StubResources, message: InputMessage) -> None:
        routed.append((resources.target, message))

    def workspace_of(message: InputMessage) -> Path:
        workspace = message.workspace
        assert workspace is not None
        return workspace

    dispatcher = WorkspaceMessageDispatcher(
        receive=fan_in.receive,
        resolver=resolver,
        workspace_of=workspace_of,
        route_one=route_one,
    )
    server = WebUIServer(
        websocket_input,
        transcript_store,
        static_dist=None,
        home_sessions_dir=paths.sessions_dir,
    )
    server.set_data_dir_name(".modex")
    server.set_workspace_index(transcript_store)
    server.set_session_store(session_store)
    server.set_session_factory(SessionIdFactory())
    server.set_agent_pool_map({"main": "main"})
    pool_store = MagicMock()
    pool_store.get.return_value = "main"
    server.set_input_pipeline(
        build_webui_pipeline(
            skill_registry=_NoSkillRegistry(),
            bot_model_config=_bot_model_config(),
        )
    )
    server.set_input_context(
        BotInputContext(
            default_pool="main",
            pool_session_store=pool_store,
            agent_pool_map={"main": "main"},
            agent_resolver=lambda pool: pool,
            transcript_store=transcript_store,
            enqueue_message=websocket_input.put_input_message,
            command_adapter=websocket_input,
            current_ws_provider=lambda: workspace_root,
        )
    )
    client = TestClient(TestServer(server.app))

    try:
        await fan_in.start()
        await client.start_server()
        websocket = await client.ws_connect("/ws")
        session_prefix = "sqlite-dispatch-regression"
        session_id = f"{session_prefix}.main"
        await websocket.send_json(
            {
                "action": "attach",
                "uuid_prefix": session_prefix,
                "pool": "main",
                "ws": str(workspace_root),
            }
        )
        attached = _unwrap_envelope(await websocket.receive_json(timeout=1))
        assert attached["event"] == WebUIEventType.ATTACHED.value

        # When
        await websocket.send_json(
            {
                "action": "send_message",
                "session_id": session_id,
                "content": "sqlite dispatch regression",
                "ws": str(workspace_root),
            }
        )
        with anyio.fail_after(1):
            await dispatcher.dispatch_once()

        # Then
        assert len(routed) == 1
        routed_workspace, routed_message = routed[0]
        assert routed_workspace == workspace_root.resolve()
        assert routed_message.workspace == workspace_root.resolve()
        assert routed_message.session.session_id == session_id
        assert routed_message.content == "sqlite dispatch regression"
    finally:
        await client.close()
        await fan_in.stop()
        await registry.evict_all()
        await persistence.close()
