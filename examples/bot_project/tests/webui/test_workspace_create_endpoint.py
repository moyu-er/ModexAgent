"""POST /api/workspace/create route handler (ticket 17).

Handler-level seam tests: the route validates the body, maps the creation
errors to HTTP statuses (409 collision / 400 validation / 503 unwired), and
returns the new workspace's path. The full creation road (write declaration
→ materialize → chat without restart) is covered by
``tests/integration/test_workspace_runtime_parallel.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.webui.routes.workspace import handle_workspace_create
from bot.workspace.dynamic_workspaces import (
    WorkspaceCreationError,
    WorkspaceCreationResult,
    WorkspaceExistsError,
)


async def _client(creator: Any) -> TestClient:
    app = web.Application()
    app["server"] = SimpleNamespace(_workspace_creator=creator, _recent_workspaces=None)
    app.router.add_post("/api/workspace/create", handle_workspace_create)
    return TestClient(TestServer(app))


@pytest.mark.asyncio
async def test_create_returns_path_on_success() -> None:
    added: list[str] = []

    async def creator(name: str, backend: str | None) -> WorkspaceCreationResult:
        assert name == "alpha"
        assert backend == "sqlite"
        return WorkspaceCreationResult(
            name="alpha",
            root=Path("/proj/subworkspace/alpha"),
            declaration_path=Path("/proj/config/scopes/workspaces/alpha.yml"),
            pools=("main",),
        )

    app = web.Application()
    app["server"] = SimpleNamespace(
        _workspace_creator=creator,
        _recent_workspaces=SimpleNamespace(add=added.append),
    )
    app.router.add_post("/api/workspace/create", handle_workspace_create)
    client = TestClient(TestServer(app))
    async with client:
        resp = await client.post(
            "/api/workspace/create", json={"name": "alpha", "backend": "sqlite"}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["success"] is True
        assert body["name"] == "alpha"
        # Path equality (not string equality): the endpoint serializes
        # native-separator paths (str(Path)) like every sibling workspace
        # route — backslashes on Windows, forward slashes elsewhere.
        assert Path(body["path"]) == Path("/proj/subworkspace/alpha")
        assert Path(body["cwd"]) == Path("/proj/subworkspace/alpha")
        assert "1 pool(s) booted" in body["notice"]
        assert [Path(p) for p in added] == [Path("/proj/subworkspace/alpha")]


@pytest.mark.asyncio
async def test_create_maps_error_statuses() -> None:
    async def collision(name: str, backend: str | None) -> WorkspaceCreationResult:
        raise WorkspaceExistsError("a workspace named 'alpha' already exists")

    async def invalid(name: str, backend: str | None) -> WorkspaceCreationResult:
        raise WorkspaceCreationError("invalid workspace name")

    async def boom(name: str, backend: str | None) -> WorkspaceCreationResult:
        raise RuntimeError("materialization exploded")

    for creator, expected in (
        (collision, 409),
        (invalid, 400),
        (boom, 500),
    ):
        client = await _client(creator)
        async with client:
            resp = await client.post(
                "/api/workspace/create", json={"name": "alpha"}
            )
            assert resp.status == expected
            body = await resp.json()
            assert body.get("error")


@pytest.mark.asyncio
async def test_create_validates_body_and_503_when_unwired() -> None:
    async def creator(name: str, backend: str | None) -> WorkspaceCreationResult:
        raise AssertionError("creator must not run for an invalid body")

    client = await _client(creator)
    async with client:
        for payload in ({}, {"name": ""}, {"name": 42}, {"name": "x", "backend": 7}):
            resp = await client.post("/api/workspace/create", json=payload)
            assert resp.status == 400
        resp = await client.post(
            "/api/workspace/create", data=b"not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400

    client = await _client(None)
    async with client:
        resp = await client.post("/api/workspace/create", json={"name": "alpha"})
        assert resp.status == 503
