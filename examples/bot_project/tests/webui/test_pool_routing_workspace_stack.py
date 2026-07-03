"""Regression tests for pool routing through the workspace stack.

The core bug: PoolSessionStore used to be per-workspace. The WebUI pipeline
writes the session→pool mapping into whatever store BotInputContext holds; the
dispatcher then routes the message into the workspace carried by the message and
that workspace's PoolRouter reads ITS OWN store. When the message workspace is
not the home workspace, the mapping is missing and routing defaults to "main".

Fix: make PoolSessionStore a service-level singleton shared by every workspace's
PoolRouter and by the WebUI pipeline.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.input_pipeline.assembly import build_webui_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.skill_parse import SkillRegistry
from bot.service.pool_router import PoolRouter, PoolSessionStore
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import _unwrap_envelope
from bot.webui.server import WebUIServer, _new_uuid_prefix
from bot.workspace.dispatch import WorkspaceMessageDispatcher
from bot.workspace.handle import PoolWorkspaceResources
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.registry import InMemoryRegistryStore, WorkspaceRegistry
from modex_agent.workspace.routing import WorkspaceResolver
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.address import AgentAddress


class _NoSkillRegistry(SkillRegistry):
    async def resolve(self, pool: str, name: str, content: str) -> None:
        return None


def _minimal_resources(
    target: Path,
    home: Path,
    *,
    shared_pool_store: PoolSessionStore | None = None,
) -> PoolWorkspaceResources:
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=home)
    ctx.paths.mkdir_skeleton()
    broker = InMemoryMessageBroker()
    main_pool = MagicMock()
    main_pool.main_agent_name = "main"
    main_pool.main_address = AgentAddress(kind="agent", name="main")
    coding_pool = MagicMock()
    coding_pool.main_agent_name = "coding"
    coding_pool.main_address = AgentAddress(kind="agent", name="coding")
    pool_router = PoolRouter(
        input_adapter=MagicMock(),
        broker=broker,
        pools={"main": main_pool, "coding": coding_pool},
        session_store=shared_pool_store
        if shared_pool_store is not None
        else PoolSessionStore(data_dir=ctx.paths.root),
        default_pool="main",
    )
    return PoolWorkspaceResources(
        target=target,
        ctx=ctx,
        overflow_store=MagicMock(),
        session_index_store=WorkspacePoolSessionStore(
            base_dir=ctx.paths.session_index_dir,
            pool_resolver=lambda s: {"main": "main", "coding": "coding"}.get(
                s.agent_name, "main"
            ),
        ),
        broker=broker,
        pool_router=pool_router,
    )


class _MinimalFactory:
    def __init__(
        self,
        home: Path,
        *,
        shared_pool_store: PoolSessionStore | None = None,
    ) -> None:
        self._home = home
        self._shared_pool_store = shared_pool_store

    async def materialize(self, ctx: WorkspaceContext) -> PoolWorkspaceResources:
        return _minimal_resources(
            ctx.target, self._home, shared_pool_store=self._shared_pool_store
        )

    async def evict(self, resources: PoolWorkspaceResources) -> None:
        await resources.broker.stop()


async def _run_dispatcher_once(
    inp: WebSocketInputAdapter,
    resolver: WorkspaceResolver[PoolWorkspaceResources],
) -> tuple[str, str]:
    """Consume one message from *inp* and return (session_prefix, resolved_pool)."""
    prefix: str = ""
    resolved: str = ""

    async def route_one(resources: PoolWorkspaceResources, message: InputMessage) -> None:
        nonlocal prefix, resolved
        prefix = message.session.session_id_prefix
        resolved = resources.pool_router._session_store.get(prefix, "main")

    dispatcher = WorkspaceMessageDispatcher(
        receive=inp.receive,
        resolver=resolver,
        workspace_of=lambda m: m.workspace,
        route_one=route_one,
    )
    await dispatcher.dispatch_once()
    return prefix, resolved


@pytest.mark.asyncio
async def test_non_home_workspace_routes_to_coding_with_shared_store() -> None:
    """With a service-level PoolSessionStore, a coding-pool conversation created
    on workspace A is correctly routed to coding even though the WebUI pipeline
    runs against the home workspace's PoolRouter.
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        ws_a = Path(tmp) / "ws_a"
        home.mkdir()
        ws_a.mkdir()

        # Service-level store — the fix.
        shared_store = PoolSessionStore(data_dir=home / ".modex")

        factory = _MinimalFactory(home=home, shared_pool_store=shared_store)
        registry = WorkspaceRegistry(
            home=home,
            data_dir_name=".modex",
            factory=factory,
            store=InMemoryRegistryStore(),
        )
        resolver = WorkspaceResolver(registry=registry)
        home_resources = await registry.materialize(registry.home_context)
        await registry.materialize(registry.get_or_open(ws_a))

        inp = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        store.set_agent_pool_map({"main": "main", "coding": "coding"})
        server = WebUIServer(
            inp,
            store,
            static_dist=None,
            data_dir=home,
            home_sessions_dir=home_resources.ctx.paths.sessions_dir,
        )
        server.set_workspace_index(store)
        server.set_data_dir_name(".modex")
        server.set_pool_agent_names(["main", "coding"])
        server.set_agent_pool_map({"main": "main", "coding": "coding"})
        server.set_agent_resolver(
            lambda p: {"main": "main", "coding": "coding"}.get(p, p)
        )
        server.set_session_factory(SessionIdFactory())
        server.set_session_store(home_resources.session_index_store)
        # Production wiring: callback writes through the home pool_router, which
        # now shares the service-level store.
        server.set_pool_switch_callback(home_resources.pool_router.set_pool)

        pipe = build_webui_pipeline(skill_registry=_NoSkillRegistry())
        server.set_input_pipeline(pipe)
        # Production wiring: pipeline ctx uses the shared store.
        ctx = BotInputContext(
            default_pool="main",
            pool_session_store=shared_store,
            agent_pool_map={"main": "main", "coding": "coding"},
            agent_resolver=lambda p: {"main": "main", "coding": "coding"}.get(p, p),
            transcript_store=store,
            enqueue_message=inp.put_input_message,
            command_adapter=inp,
            session_factory=SessionIdFactory(),
            current_ws_provider=lambda: ws_a,
        )
        server.set_input_context(ctx)

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            conv_prefix = _new_uuid_prefix()
            await ws.send_json({
                "action": "attach",
                "uuid_prefix": conv_prefix,
                "pool": "coding",
            })
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"
            session_id = attached["session_id"]

            await ws.send_json({
                "action": "send_message",
                "session_id": session_id,
                "content": "hello coding",
            })
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"

            pool_file = shared_store._dir / f"{conv_prefix}.json"
            assert pool_file.exists(), (
                f"BUG: {pool_file} was not created. "
                f"Shared pool_session_store did not persist the coding mapping."
            )
            assert json.loads(pool_file.read_text())["pool"] == "coding"

            prefix, resolved = await _run_dispatcher_once(inp, resolver)
            assert prefix == conv_prefix
            assert resolved == "coding", (
                f"Dispatcher routed {prefix!r} to {resolved!r}, not coding"
            )
        finally:
            await client.close()
            await home_resources.broker.stop()


@pytest.mark.asyncio
async def test_home_workspace_coding_conversation_routes_to_coding() -> None:
    """A coding-pool conversation created on the home workspace must still be
    routed to coding when using the shared service-level store.
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()

        shared_store = PoolSessionStore(data_dir=home / ".modex")
        factory = _MinimalFactory(home=home, shared_pool_store=shared_store)
        registry = WorkspaceRegistry(
            home=home,
            data_dir_name=".modex",
            factory=factory,
            store=InMemoryRegistryStore(),
        )
        resolver = WorkspaceResolver(registry=registry)
        home_resources = await registry.materialize(registry.home_context)

        inp = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        store.set_agent_pool_map({"main": "main", "coding": "coding"})
        server = WebUIServer(
            inp,
            store,
            static_dist=None,
            data_dir=home,
            home_sessions_dir=home_resources.ctx.paths.sessions_dir,
        )
        server.set_workspace_index(store)
        server.set_data_dir_name(".modex")
        server.set_pool_agent_names(["main", "coding"])
        server.set_agent_pool_map({"main": "main", "coding": "coding"})
        server.set_agent_resolver(
            lambda p: {"main": "main", "coding": "coding"}.get(p, p)
        )
        server.set_session_factory(SessionIdFactory())
        server.set_session_store(home_resources.session_index_store)
        server.set_pool_switch_callback(home_resources.pool_router.set_pool)

        pipe = build_webui_pipeline(skill_registry=_NoSkillRegistry())
        server.set_input_pipeline(pipe)
        ctx = BotInputContext(
            default_pool="main",
            pool_session_store=shared_store,
            agent_pool_map={"main": "main", "coding": "coding"},
            agent_resolver=lambda p: {"main": "main", "coding": "coding"}.get(p, p),
            transcript_store=store,
            enqueue_message=inp.put_input_message,
            command_adapter=inp,
            session_factory=SessionIdFactory(),
            current_ws_provider=lambda: home,
        )
        server.set_input_context(ctx)

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            conv_prefix = _new_uuid_prefix()
            await ws.send_json({
                "action": "attach",
                "uuid_prefix": conv_prefix,
                "pool": "coding",
            })
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"
            session_id = attached["session_id"]

            await ws.send_json({
                "action": "send_message",
                "session_id": session_id,
                "content": "hello coding",
            })
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"

            pool_file = shared_store._dir / f"{conv_prefix}.json"
            assert pool_file.exists(), (
                f"BUG: {pool_file} was not created. "
                f"Shared pool_session_store did not persist the coding mapping."
            )
            assert json.loads(pool_file.read_text())["pool"] == "coding"

            prefix, resolved = await _run_dispatcher_once(inp, resolver)
            assert prefix == conv_prefix
            assert resolved == "coding", (
                f"Dispatcher routed {prefix!r} to {resolved!r}, not coding"
            )
        finally:
            await client.close()
            await home_resources.broker.stop()
