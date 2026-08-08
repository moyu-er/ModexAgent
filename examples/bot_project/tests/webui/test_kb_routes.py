from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.kb.builder import build_default_kb_provider
from bot.kb.models import KbFilter, KbSearchResult, KbUpsertRequest
from bot.kb.provider import KbProvider
from bot.persistence.migration import BotWorkspaceMigrationRunner
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer
from bot.workspace.handle import PoolWorkspaceResources

from modex_agent.persistence import ConnectionManager, DatabaseKind


class ProviderFailureError(RuntimeError):
    def __str__(self) -> str:
        return "FTS5 upsert KbFilter"


@pytest.fixture
async def kb_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[TestClient, KbProvider, str]]:
    connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await connection.open()
    await BotWorkspaceMigrationRunner(connection).run_pending()
    provider = await build_default_kb_provider(connection)

    resources = MagicMock(spec=PoolWorkspaceResources)
    resources.kb_provider = provider
    workspace = str(tmp_path)

    server = WebUIServer(
        WebSocketInputAdapter(),
        WorkspaceScopedTranscriptStore(data_dir_name=".modex"),
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex" / "sessions",
    )
    server.set_graph_workspace_resolver(
        lambda requested: resources if requested == workspace else None
    )
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        yield client, provider, workspace
    finally:
        await client.close()
        await connection.close()


def _body(
    action: str,
    query_or_key: str | None = None,
    value: str | None = None,
) -> dict[str, str | int | None | dict[str, str | None]]:
    return {
        "action": action,
        "query_or_key": query_or_key,
        "value": value,
        "filter": {
            "task_id": None,
            "session_id": None,
            "category": None,
        },
        "limit": 20,
    }


async def test_search_returns_formatted_text(
    kb_client: tuple[TestClient, KbProvider, str],
) -> None:
    client, provider, workspace = kb_client
    await provider.upsert(
        KbUpsertRequest(key="deploy-steps", value="production deployment workflow")
    )

    response = await client.post(
        "/api/control/kb",
        params={"workspace": workspace},
        json=_body("search", "deployment"),
    )

    assert response.status == 200
    payload = await response.json()
    search_result = payload["result"]
    assert isinstance(search_result, str)
    assert "Found 1 result(s):" in search_result
    assert "[deploy-steps]" in search_result
    assert "score:" in search_result
    for internal_field in (
        "entry_id",
        "task_id",
        "session_id",
        "created_at",
        "updated_at",
    ):
        assert internal_field not in search_result


async def test_set_returns_formatted_confirmation(
    kb_client: tuple[TestClient, KbProvider, str],
) -> None:
    client, _, workspace = kb_client
    body = _body("set", "architecture", "Keep modules deep")
    body["filter"] = {
        "task_id": "task-1",
        "session_id": "session-1",
        "category": "project",
    }

    response = await client.post(
        "/api/control/kb", params={"workspace": workspace}, json=body
    )

    assert response.status == 200
    result = (await response.json())["result"]
    assert result == "Saved: architecture (category: project)"


async def test_get_returns_formatted_text(
    kb_client: tuple[TestClient, KbProvider, str],
) -> None:
    client, provider, workspace = kb_client
    await provider.upsert(KbUpsertRequest(key="architecture", value="Keep modules deep"))

    response = await client.post(
        "/api/control/kb",
        params={"workspace": workspace},
        json=_body("get", "architecture"),
    )

    assert response.status == 200
    result = (await response.json())["result"]
    assert isinstance(result, str)
    assert "[architecture]" in result
    assert "--------------------------------------------------" in result
    assert "Keep modules deep" in result


async def test_delete_returns_deleted_status(
    kb_client: tuple[TestClient, KbProvider, str],
) -> None:
    client, provider, workspace = kb_client
    await provider.upsert(KbUpsertRequest(key="obsolete", value="remove me"))

    response = await client.post(
        "/api/control/kb",
        params={"workspace": workspace},
        json=_body("delete", "obsolete"),
    )

    assert response.status == 200
    assert (await response.json())["result"] == "Deleted: obsolete"


async def test_delete_returns_not_found_text_when_missing(
    kb_client: tuple[TestClient, KbProvider, str],
) -> None:
    client, _, workspace = kb_client

    response = await client.post(
        "/api/control/kb",
        params={"workspace": workspace},
        json=_body("delete", "missing"),
    )

    assert response.status == 200
    assert (await response.json())["result"] == "Not found: missing"


async def test_list_returns_formatted_text(
    kb_client: tuple[TestClient, KbProvider, str],
) -> None:
    client, provider, workspace = kb_client
    await provider.upsert(KbUpsertRequest(key="alpha", value="first"))
    await provider.upsert(KbUpsertRequest(key="beta", value="second"))

    response = await client.post(
        "/api/control/kb",
        params={"workspace": workspace},
        json=_body("list"),
    )

    assert response.status == 200
    result = (await response.json())["result"]
    assert isinstance(result, str)
    assert result == "2 key(s):\n- alpha\n- beta"


async def test_missing_action_returns_400(
    kb_client: tuple[TestClient, KbProvider, str],
) -> None:
    client, _, workspace = kb_client
    body = _body("list")
    del body["action"]

    response = await client.post(
        "/api/control/kb", params={"workspace": workspace}, json=body
    )

    assert response.status == 400


async def test_invalid_action_returns_400(
    kb_client: tuple[TestClient, KbProvider, str],
) -> None:
    client, _, workspace = kb_client

    response = await client.post(
        "/api/control/kb",
        params={"workspace": workspace},
        json=_body("unknown"),
    )

    assert response.status == 400


async def test_missing_workspace_returns_400(
    kb_client: tuple[TestClient, KbProvider, str],
) -> None:
    client, _, _ = kb_client

    response = await client.post("/api/control/kb", json=_body("list"))

    assert response.status == 400


async def test_provider_failure_returns_generic_500(
    kb_client: tuple[TestClient, KbProvider, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, provider, workspace = kb_client

    async def fail_search(
        query: str, filter: KbFilter, limit: int
    ) -> list[KbSearchResult]:
        raise ProviderFailureError

    monkeypatch.setattr(provider, "search", fail_search)

    response = await client.post(
        "/api/control/kb",
        params={"workspace": workspace},
        json=_body("search", "deployment"),
    )

    assert response.status == 500
    error = (await response.json())["error"]
    assert all(term not in error for term in ("FTS5", "upsert", "KbFilter"))
