"""WebUI REST routes: the surviving pool-config surface (ticket 11).

The pool.yml CRUD routes (create/read/write/delete pool + bidirectional
peers + roster-field round-trips) retired with the legacy roster road —
pool trees are edited through the scope declaration editor
(``PUT /api/scope/declaration``, covered by the scope routes tests). What
remains here: the declaration-backed pool LISTING, the MCP registry CRUD,
the skills CRUD, and the 503 wiring guards.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import aiohttp
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.config.mcp_registry import REGISTRY_PATH
from bot.config.prompt_store import PromptStore
from bot.config.skills_store import SkillsStore
from bot.service.pool_config_controller import PoolConfigController
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer

_BOT_PROJECT = Path(__file__).resolve().parents[2]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))


# ─── fixtures ────────────────────────────────────────────────────────────────


def _seed_declaration(base: Path, pools: dict) -> Path:
    """Write a workspace declaration (the pool source the controller reads)."""
    scopes_dir = base / "config" / "scopes"
    scopes_dir.mkdir(parents=True, exist_ok=True)
    p = scopes_dir / "bot.yml"
    p.write_text(
        yaml.safe_dump({"workspace": {"name": "w", "pools": pools}}, sort_keys=False),
        encoding="utf-8",
    )
    return p


def _make_controller(tmp_path: Path) -> PoolConfigController:
    return PoolConfigController(
        declaration_path=tmp_path / "config" / "scopes" / "bot.yml",
        skills_store=SkillsStore(base_dir=tmp_path, user_global_dir=tmp_path / "user_skills"),
        prompt_store=PromptStore(base_dir=tmp_path),
        mcp_registry_path=tmp_path / REGISTRY_PATH,
    )


def _make_client(controller: PoolConfigController, tmp_path: Path) -> TestClient:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    server.set_pool_config_controller(controller)
    return TestClient(TestServer(server.app))


# ─── pools (declaration-backed listing) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_list_pools_reads_the_declaration(tmp_path: Path) -> None:
    _seed_declaration(
        tmp_path,
        {
            "main": {
                "agents": {
                    "main": {
                        "description": "the main root",
                        "agents": {"worker": {"description": "a subagent"}},
                    },
                }
            },
            "opencode": {
                "agents": {
                    "opencode": {
                        "description": "external root",
                        "execution_strategy": "external",
                        "provider_kind": "opencode",
                    }
                }
            },
        },
    )
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/pools")
        assert resp.status == 200, await resp.text()
        pools = await resp.json()
        assert [p["name"] for p in pools] == ["main", "opencode"]
        assert pools[0]["root_agent_name"] == "main"
        assert pools[0]["subagent_count"] == 1
        assert pools[1]["root_agent_name"] == "opencode"
        assert pools[1]["subagent_count"] == 0
    finally:
        await client.close()


# ─── mcp ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_empty_mcp(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/mcp")
        assert resp.status == 200
        assert await resp.json() == {}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upsert_and_delete_mcp(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        entry = {"type": "stdio", "command": "npx", "args": ["-y", "fetch"]}
        resp = await client.post("/api/mcp/fetch", json=entry)
        assert resp.status == 200, await resp.text()
        got = await (await client.get("/api/mcp")).json()
        assert "fetch" in got
        assert got["fetch"]["command"] == "npx"

        resp = await client.delete("/api/mcp/fetch")
        assert resp.status == 200
        got = await (await client.get("/api/mcp")).json()
        assert "fetch" not in got
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_referenced_mcp_succeeds(tmp_path: Path) -> None:
    """Deleting a global MCP server succeeds unconditionally; a stale
    declaration reference surfaces loudly at the next boot's workspace
    MCP-set validation (the legacy lazy pool-tree filtering died with the
    legacy road)."""
    _seed_declaration(
        tmp_path,
        {
            "main": {
                "agents": {
                    "main": {"description": "root", "mcp": ["fetch"]},
                }
            }
        },
    )
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        await client.post("/api/mcp/fetch", json={"type": "stdio", "command": "x"})
        resp = await client.delete("/api/mcp/fetch")
        assert resp.status == 200, await resp.text()
        assert (await resp.json()) == {"deleted": "fetch"}
        assert "fetch" not in await (await client.get("/api/mcp")).json()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_unknown_mcp_404(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/mcp/ghost")
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_env_placeholder_round_trip_uninterpolated(tmp_path: Path) -> None:
    """${ENV} placeholders in url/headers survive POST→GET uninterpolated.

    The registry persists RAW values (interpolation happens at load in the
    existing MCP runtime), so the store invariant is: write-then-read returns
    the placeholder verbatim, never the expanded shell value.
    """
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        entry = {
            "type": "streamableHttp",
            "url": "https://${MY_VAR}/hook",
            "headers": {"X-Token": "${SECRET_TOKEN}"},
        }
        resp = await client.post("/api/mcp/srv", json=entry)
        assert resp.status == 200, await resp.text()
        got = await (await client.get("/api/mcp")).json()
        assert "srv" in got
        assert got["srv"]["url"] == "https://${MY_VAR}/hook"
        assert got["srv"]["headers"]["X-Token"] == "${SECRET_TOKEN}"
    finally:
        await client.close()


# ─── skills ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_skill_json_then_list_and_assign(tmp_path: Path) -> None:
    _seed_declaration(tmp_path, {"main": {"agents": {"main": {"description": "root"}}}})
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        files = {
            "SKILL.md": base64.b64encode(b"# hello\n").decode(),
            "sub/a.txt": base64.b64encode(b"aa").decode(),
        }
        resp = await client.post("/api/skills", json={"name": "hello", "files": files})
        assert resp.status == 200, await resp.text()

        listed = await (await client.get("/api/skills")).json()
        names = [s["name"] for s in listed]
        assert "hello" in names

        # Agent skills empty before assignment.
        got = await (await client.get("/api/pools/main/agents/main/skills")).json()
        assert got == []

        # Assign -> shows up, source=global.
        resp = await client.post("/api/pools/main/agents/main/skills/hello")
        assert resp.status == 200, await resp.text()
        got = await (await client.get("/api/pools/main/agents/main/skills")).json()
        assert any(s["name"] == "hello" and s["source"] == "global" for s in got)

        # Unassign.
        resp = await client.delete("/api/pools/main/agents/main/skills/hello")
        assert resp.status == 200, await resp.text()
        got = await (await client.get("/api/pools/main/agents/main/skills")).json()
        assert got == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upload_skill_multipart(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        data = aiohttp.FormData()
        data.add_field("name", "mp")
        data.add_field(
            "files",
            base64.b64encode(b"# mp\n").decode(),
            filename="SKILL.md",
            content_type="application/octet-stream",
        )
        resp = await client.post("/api/skills", data=data)
        assert resp.status == 200, await resp.text()
        listed = await (await client.get("/api/skills")).json()
        assert any(s["name"] == "mp" for s in listed)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_skill(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        files = {"SKILL.md": base64.b64encode(b"# xx\n").decode()}
        await client.post("/api/skills", json={"name": "xx", "files": files})
        resp = await client.delete("/api/skills/xx")
        assert resp.status == 200, await resp.text()
        listed = await (await client.get("/api/skills")).json()
        assert not any(s["name"] == "xx" for s in listed)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_restart_required_false_after_global_skill_upload(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        files = {"SKILL.md": base64.b64encode(b"# hot\n").decode()}
        await client.post("/api/skills", json={"name": "hot", "files": files})
        assert controller.restart_required is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_restart_required_true_after_skill_assign(tmp_path: Path) -> None:
    _seed_declaration(tmp_path, {"main": {"agents": {"main": {"description": "root"}}}})
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        files = {"SKILL.md": base64.b64encode(b"# hot\n").decode()}
        await client.post("/api/skills", json={"name": "hot", "files": files})
        assert controller.restart_required is False
        resp = await client.post("/api/pools/main/agents/main/skills/hot")
        assert resp.status == 200, await resp.text()
        assert controller.restart_required is True
    finally:
        await client.close()


# ─── wiring guards ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_endpoints_return_503_when_controller_not_set(tmp_path: Path) -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    # NOTE: no set_pool_config_controller call.
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        assert (await client.get("/api/pools")).status == 503
        assert (await client.get("/api/mcp")).status == 503
        assert (await client.get("/api/skills")).status == 503
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_skill_route_traversal_rejected(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/pools/main/agents/main/skills/..%2F..%2Fetc")
        assert resp.status in (400, 404)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_route_traversal_rejected(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/mcp/..%2F..%2Fetc")
        assert resp.status in (400, 404)
    finally:
        await client.close()
