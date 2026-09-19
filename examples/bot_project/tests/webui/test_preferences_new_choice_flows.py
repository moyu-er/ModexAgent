"""PA-06/PA-07 end-to-end: ONE preferences object drives every new-choice flow.

Integration through the REAL seams: ConfigController (the REST config
surface), the WebUIServer REST/WS handlers, and the input pipeline's S5
stage over a real routing store. Saving a preference via the config surface
is immediately visible to new REST creates, new WS attaches (missing pool),
and fresh IM conversations — while existing stored routes never move.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.config.domains.personal_assistant import PersonalAssistantPreferences
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import ResolvePoolStage, RoutingMeta
from bot.service.config_controller import ConfigController
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer

from modex_agent.core.session_id import encode_snowflake
from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore


def _make_server(
    tmp_path: Path,
    prefs: PersonalAssistantPreferences,
    routing_store: LocalFilePoolRoutingStore,
) -> WebUIServer:
    inp = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    server.set_data_dir_name(".modex")
    server.set_pool_agent_names(["default", "coder"])
    server.set_agent_resolver(lambda pool_name: pool_name)
    server.set_available_pools_provider(lambda: {"default", "coder"})
    server.set_personal_assistant_preferences(prefs)
    server.set_config_controller(ConfigController(domains=(prefs.domain,)))
    server.set_pool_switch_callback(routing_store.set_pool)
    server.set_pool_resolver(routing_store.get_pool)
    return server


@pytest.mark.asyncio
async def test_config_put_updates_rest_create_and_ws_attach_and_im_default(
    tmp_path: Path,
) -> None:
    """One prefs object: a PUT through the real config surface is visible to
    every new-choice flow without restart."""
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    routing_store = LocalFilePoolRoutingStore(tmp_path / "routing")

    # ── Baseline: pristine preference ('default') serves REST create ──
    server = _make_server(tmp_path, prefs, routing_store)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={})
        assert resp.status == 200
        assert (await resp.json())["pool"] == "default"

        # ── PUT through the REAL config controller surface ──
        resp = await client.put(
            "/api/config/personal_assistant", json={"default_pool": "coder"}
        )
        assert resp.status == 200
        payload = await resp.json()
        assert payload["values"]["default_pool"] == "coder"
        # immediate-effect domain: never claims a restart is required
        assert payload["restart_required"] is False

        # ── New REST create reflects the PUT immediately ──
        resp = await client.post("/api/sessions", json={})
        assert resp.status == 200
        assert (await resp.json())["pool"] == "coder"

        # ── New WS attach WITHOUT a pool reflects the PUT ──
        ws = await client.ws_connect("/ws")
        await ws.send_json(
            {"action": "attach", "uuid_prefix": "web-newconv", "ws": ""}
        )
        attached = await ws.receive_json()
        assert attached["event_type"] == "attached"
        assert attached["pool"] == "coder"
        # attach pinned the route through the pool-switch callback
        assert routing_store.get_pool("web-newconv") == "coder"
        await ws.close()

        # ── Existing stored route is untouched by the PUT (V12) ──
        routing_store.set_pool("conv-old", "default")
        assert routing_store.get_pool("conv-old") == "default"
    finally:
        await client.close()

    # ── IM first message (S5) reflects the same preference object ──
    ctx = BotInputContext(
        default_pool=None,
        default_pool_provider=lambda: prefs.preferred_pool()
        if prefs.preferred_pool() in {"default", "coder"}
        else None,
        available_pools=lambda: {"default", "coder"},
        pool_session_store=LocalFilePoolRoutingStore(tmp_path / "routing"),
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )
    from modex_agent.input_pipeline.envelope import UserInputEnvelope

    env = UserInputEnvelope(
        external_id="im-user", content="hi", channel="qq", explicit_pool=None
    )
    await ResolvePoolStage().process(env, ctx)
    assert env.metadata[RoutingMeta.RESOLVED_POOL] == "coder"
    prefix = encode_snowflake("im-user")
    assert ctx.pool_session_store.get_pool(prefix) == "coder"


@pytest.mark.asyncio
async def test_ws_attach_without_pool_and_no_preference_errors_with_reason(
    tmp_path: Path,
) -> None:
    """No valid preference (unreadable file) + attach without pool: the
    client gets an actionable error envelope — never a silent main/first."""
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    (tmp_path / "personal_assistant.yml").write_text(
        "default_pool: [broken\n", encoding="utf-8"
    )
    routing_store = LocalFilePoolRoutingStore(tmp_path / "routing")
    server = _make_server(tmp_path, prefs, routing_store)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "uuid_prefix": "web-x", "ws": ""})
        err = await ws.receive_json()
        assert err["event_type"] == "error"
        assert "select a pool" in err["payload"]["message"]
        # nothing was routed
        assert routing_store.get_pool("web-x") is None
        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_preference_put_with_unavailable_pool_keeps_flows_requiring_choice(
    tmp_path: Path,
) -> None:
    """PUT of a pool that is not declared: the write REJECTS at validation?
    No — pool validity is availability-scoped, not schema-scoped. The saved
    preference then makes every new-choice entry report selection-needed
    (409 / error envelope); the explicit choice still works."""
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    routing_store = LocalFilePoolRoutingStore(tmp_path / "routing")
    server = _make_server(tmp_path, prefs, routing_store)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Save a preference for an undeclared pool (schema-valid; availability
        # is validated at selection time, not save time).
        resp = await client.put(
            "/api/config/personal_assistant", json={"default_pool": "ghost"}
        )
        assert resp.status == 200

        # REST create: 409 with the reason
        resp = await client.post("/api/sessions", json={})
        assert resp.status == 409
        assert "ghost" in (await resp.json())["error"]

        # Explicit choice still works (availability-checked)
        resp = await client.post("/api/sessions", json={"pool": "coder"})
        assert resp.status == 200
        assert (await resp.json())["pool"] == "coder"

        # Explicit unknown pool still 400
        resp = await client.post("/api/sessions", json={"pool": "ghost"})
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_workspace_endpoint_reports_same_preference_object_values(
    tmp_path: Path,
) -> None:
    """/api/workspace reports the SAME preference object's values as the
    config surface — one object, no divergence."""
    target = tmp_path / "mydir"
    target.mkdir()
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    routing_store = LocalFilePoolRoutingStore(tmp_path / "routing")
    server = _make_server(tmp_path, prefs, routing_store)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.put(
            "/api/config/personal_assistant",
            json={"default_pool": "coder", "default_workspace": str(target)},
        )
        assert resp.status == 200
        resp = await client.get("/api/workspace")
        assert resp.status == 200
        data = await resp.json()
        assert data["default_pool"] == "coder"
        assert Path(data["default_workspace"]) == target.resolve()
    finally:
        await client.close()


@pytest.mark.parametrize("preferred", ["default", "coder"])
async def test_attaching_unattributed_history_never_pins_the_new_default(tmp_path: Path, preferred: str) -> None:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore

    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    routes = LocalFilePoolRoutingStore(tmp_path / "routing")
    server = _make_server(tmp_path, prefs, routes)
    store = LocalFileSessionStore(tmp_path / "index")
    await store.save(SessionInfo(session_id="orphan.coder", agent_name="coder"))
    server.set_session_store(store)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.put("/api/config/personal_assistant", json={"default_pool": preferred})
        assert response.status == 200
        async with client.ws_connect("/ws") as ws:
            await ws.send_json({"action": "attach", "session_id": "orphan.coder"})
            attached = await ws.receive_json()
            assert attached["event_type"] == "attached"
            assert attached["pool"] == ""
            assert routes.get_pool("orphan") is None
