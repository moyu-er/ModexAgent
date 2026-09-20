"""PA-07: POST /api/sessions default-pool selection through the unified rule.

The REST create entry consumes the same selection decision as every other
entry (S5, WS attach): explicit body pool wins; absent body pool resolves via
the unified default (preference → declared-first), never a hardcoded ``main``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.config.domains.personal_assistant import PersonalAssistantPreferences
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer


def _make_client(
    tmp_path: Path,
    prefs: PersonalAssistantPreferences,
    *,
    pool_names: list[str],
) -> TestClient:
    inp = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    server.set_data_dir_name(".modex")
    server.set_pool_agent_names(pool_names)
    server.set_agent_resolver(lambda pool_name: pool_name)
    server.set_available_pools_provider(lambda: set(pool_names))
    server.set_personal_assistant_preferences(prefs)
    return TestClient(TestServer(server.app))


async def _create(client: TestClient, body: dict[str, object] | None) -> dict[str, object]:
    resp = await client.post("/api/sessions", json=body or {})
    assert resp.status == 200
    data: dict[str, object] = await resp.json()
    return data


@pytest.mark.asyncio
async def test_create_without_pool_uses_saved_preference(tmp_path: Path) -> None:
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    prefs.save(default_workspace=None, default_pool="coder")
    client = _make_client(tmp_path, prefs, pool_names=["default", "coder"])
    await client.start_server()
    try:
        data = await _create(client, None)
        assert data["pool"] == "coder"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_without_pool_pristine_preference_uses_default(
    tmp_path: Path,
) -> None:
    """The shipped pristine preference value 'default' is a real preference:
    it selects the declared 'default' pool when available."""
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    client = _make_client(tmp_path, prefs, pool_names=["default", "coder"])
    await client.start_server()
    try:
        data = await _create(client, None)
        assert data["pool"] == "default"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_without_pool_and_without_preference_reports_choice_needed(
    tmp_path: Path,
) -> None:
    """DESIGN §3.3: no valid preference ⇒ 409 + reason. The server never
    picks the first declared pool / main on the user's behalf."""
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    (tmp_path / "personal_assistant.yml").write_text(
        "default_pool: [broken\n", encoding="utf-8"
    )
    client = _make_client(tmp_path, prefs, pool_names=["default", "coder"])
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={})
        assert resp.status == 409
        data = await resp.json()
        assert "select a pool" in data["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_explicit_pool_wins_over_preference(tmp_path: Path) -> None:
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    prefs.save(default_workspace=None, default_pool="coder")
    client = _make_client(tmp_path, prefs, pool_names=["default", "coder"])
    await client.start_server()
    try:
        data = await _create(client, {"pool": "default"})
        assert data["pool"] == "default"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_when_preferred_pool_unavailable_reports_selection_needed(
    tmp_path: Path,
) -> None:
    """Preferred pool not in the available set: 409 with a reason — the client
    must prompt; the server never substitutes another pool silently."""
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    prefs.save(default_workspace=None, default_pool="ghost")
    client = _make_client(tmp_path, prefs, pool_names=["default", "coder"])
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={})
        assert resp.status == 409
        data = await resp.json()
        assert "ghost" in data["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_explicit_unknown_pool_rejected(tmp_path: Path) -> None:
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    client = _make_client(tmp_path, prefs, pool_names=["default", "coder"])
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={"pool": "ghost"})
        assert resp.status == 400
    finally:
        await client.close()
