"""Tests for the global prompts REST API (``GET /api/prompts`` and
``GET /api/prompts/{name}``).

Mirrors the fixture pattern in :mod:`tests.webui.test_pool_routes`: a
``PoolConfigController`` wired to ``tmp_path``-backed stores, driven through
``aiohttp.test_utils``. The real ``agents/`` tree is never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

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


def _seed_md(tmp_path: Path, name: str, content: str = "body") -> Path:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    p = agents_dir / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ─── list ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_only_agent_mds_sorted(tmp_path: Path) -> None:
    _seed_md(tmp_path, "zeta", "z body")
    _seed_md(tmp_path, "alpha", "a body")
    _seed_md(tmp_path, "main", "main body")
    # Non-agent-name files that must be EXCLUDED.
    (tmp_path / "agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agents" / "AGENTS.md").write_text("repo root", encoding="utf-8")
    (tmp_path / "agents" / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "agents" / "UPPER.md").write_text("upper", encoding="utf-8")

    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/prompts")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        names = [p["name"] for p in data]
        # Sorted alphabetically; non-matching stems excluded.
        assert names == ["alpha", "main", "zeta"]
        # Each entry carries size_bytes + mtime (ISO 8601 string).
        for entry in data:
            assert isinstance(entry["size_bytes"], int)
            assert entry["size_bytes"] > 0
            assert isinstance(entry["mtime"], str)
            assert "T" in entry["mtime"]  # ISO 8601
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_empty_when_no_agents_dir(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/prompts")
        assert resp.status == 200, await resp.text()
        assert await resp.json() == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_excludes_agents_md_specifically(tmp_path: Path) -> None:
    """AGENTS.md is a project-root convention, not an agent prompt."""
    _seed_md(tmp_path, "main", "main body")
    (tmp_path / "agents" / "AGENTS.md").write_text("root", encoding="utf-8")

    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/prompts")
        assert resp.status == 200
        names = [p["name"] for p in await resp.json()]
        assert "AGENTS" not in names
        assert "agents" not in names
        assert names == ["main"]
    finally:
        await client.close()


# ─── read ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_returns_content(tmp_path: Path) -> None:
    _seed_md(tmp_path, "main", "# main prompt\nYou are helpful.")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/prompts/main")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["name"] == "main"
        assert "You are helpful." in data["content"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_read_404_when_missing_does_not_seed(tmp_path: Path) -> None:
    """GET /api/prompts/{name} must NOT seed — distinct from the legacy
    pool-scoped read_prompt which creates the file on first read."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/prompts/ghost")
        assert resp.status == 404
        data = await resp.json()
        assert "unknown prompt" in data["error"]
        # File was NOT created (no seeding).
        assert not (agents_dir / "ghost.md").exists()
    finally:
        await client.close()


# ─── name validation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_rejects_invalid_name_400(tmp_path: Path) -> None:
    """Names not matching ^[a-z][a-z0-9_-]+$ are rejected with 400, not 404."""
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        # Uppercase letters fail the regex.
        resp = await client.get("/api/prompts/BadName")
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "validation"
        assert "name" in data["fields"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_read_rejects_uppercase_agents_md(tmp_path: Path) -> None:
    """Even if a file exists on disk, an invalid name is rejected before read."""
    (tmp_path / "agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agents" / "AGENTS.md").write_text("root", encoding="utf-8")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/prompts/AGENTS")
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_read_traversal_rejected(tmp_path: Path) -> None:
    """Path-traversal attempts are rejected by the name regex (400/404)."""
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/prompts/..%2F..%2Fetc")
        assert resp.status in (400, 404)
    finally:
        await client.close()


# ─── controller-not-wired degradation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompts_endpoints_503_without_controller(tmp_path: Path) -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        assert (await client.get("/api/prompts")).status == 503
        assert (await client.get("/api/prompts/main")).status == 503
    finally:
        await client.close()


# ─── PUT (upsert) ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_creates_prompt_when_absent(tmp_path: Path) -> None:
    """PUT /api/prompts/{name} is upsert — creates the file if absent (200)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.put(
            "/api/prompts/newagent",
            json={"content": "# New\nYou are new."},
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["name"] == "newagent"
        assert data["content"] == "# New\nYou are new."
        # File was created on disk.
        assert (agents_dir / "newagent.md").read_text(encoding="utf-8") == "# New\nYou are new."
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_updates_existing_prompt(tmp_path: Path) -> None:
    """PUT on an existing prompt overwrites its content (upsert, no 409)."""
    _seed_md(tmp_path, "main", "old body")
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.put(
            "/api/prompts/main",
            json={"content": "new body"},
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["content"] == "new body"
        assert (tmp_path / "agents" / "main.md").read_text(encoding="utf-8") == "new body"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_rejects_invalid_name_400(tmp_path: Path) -> None:
    """Names not matching ^[a-z][a-z0-9_-]+$ are rejected with 400."""
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.put(
            "/api/prompts/BadName",
            json={"content": "x"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "validation"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_rejects_missing_content_400(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.put("/api/prompts/main", json={})
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "validation"
        assert "content" in data["fields"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_sets_restart_required(tmp_path: Path) -> None:
    """PUT marks the `prompt` dirty class so restart_required becomes True."""
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        assert controller.restart_required is False
        resp = await client.put(
            "/api/prompts/main",
            json={"content": "hello"},
        )
        assert resp.status == 200, await resp.text()
        assert controller.restart_required is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_preserves_trailing_newline_and_utf8(tmp_path: Path) -> None:
    """The editor preserves trailing newline and UTF-8 encoding on write."""
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        body = "You are 辅助.\n"
        resp = await client.put("/api/prompts/utf8", json={"content": body})
        assert resp.status == 200, await resp.text()
        written = (tmp_path / "agents" / "utf8.md").read_text(encoding="utf-8")
        assert written == body
        assert written.endswith("\n")
    finally:
        await client.close()


# ─── POST (create) ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_creates_prompt_with_default_seed(tmp_path: Path) -> None:
    """POST /api/prompts with {name} only seeds DEFAULT_PROMPT_SEED and 201."""
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.post("/api/prompts", json={"name": "newone"})
        assert resp.status == 201, await resp.text()
        data = await resp.json()
        assert data["name"] == "newone"
        assert data["content"] == PromptStore.DEFAULT_PROMPT_SEED
        assert (tmp_path / "agents" / "newone.md").read_text(encoding="utf-8") == PromptStore.DEFAULT_PROMPT_SEED
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_creates_prompt_with_explicit_content(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.post(
            "/api/prompts",
            json={"name": "custom", "content": "# Custom\nBody."},
        )
        assert resp.status == 201, await resp.text()
        data = await resp.json()
        assert data["content"] == "# Custom\nBody."
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_duplicate_with_409(tmp_path: Path) -> None:
    """POST on an existing name returns 409 (not 400, not 201)."""
    _seed_md(tmp_path, "main", "existing")
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.post("/api/prompts", json={"name": "main"})
        assert resp.status == 409, await resp.text()
        data = await resp.json()
        assert data["error"] == "exists"
        assert data["name"] == "main"
        # Original content untouched.
        assert (tmp_path / "agents" / "main.md").read_text(encoding="utf-8") == "existing"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_invalid_name_400(tmp_path: Path) -> None:
    """Invalid names (uppercase, digits-first, dots, slashes) return 400."""
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        for bad in ("BadName", "1starts-digit", "has.dot", "has/slash"):
            resp = await client.post("/api/prompts", json={"name": bad})
            assert resp.status == 400, f"{bad!r} should be 400, got {resp.status}"
            data = await resp.json()
            assert data["error"] == "validation"
            assert "name" in data["fields"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_missing_name_400(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.post("/api/prompts", json={})
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "validation"
        assert "name" in data["fields"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_sets_restart_required(tmp_path: Path) -> None:
    """Create marks the `prompt` dirty class so restart_required becomes True."""
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        assert controller.restart_required is False
        resp = await client.post("/api/prompts", json={"name": "fresh"})
        assert resp.status == 201, await resp.text()
        assert controller.restart_required is True
    finally:
        await client.close()


# ─── DELETE (reference-checked) ───────────────────────────────────────────────


_POOLS_BY_TMP: dict[str, dict[str, dict[str, Any]]] = {}


def _seed_pool_yml(
    tmp_path: Path,
    pool_name: str,
    *,
    main_prompt_name: str | None = None,
    main_agent_name: str | None = None,
    subagents: list[dict[str, Any]] | None = None,
) -> None:
    """Add one pool to the tmp declaration (accumulates across calls)."""
    pools = _POOLS_BY_TMP.setdefault(str(tmp_path), {})
    agent_name = main_agent_name or pool_name
    root: dict[str, Any] = {"description": f"{agent_name} root"}
    if main_prompt_name is not None:
        root["prompt_name"] = main_prompt_name
    for sub in subagents or []:
        sub_body: dict[str, Any] = {"description": f"{sub['agent_name']} sub"}
        if sub.get("prompt_name") is not None:
            sub_body["prompt_name"] = sub["prompt_name"]
        root.setdefault("agents", {})[sub["agent_name"]] = sub_body
    pools[pool_name] = {"agents": {agent_name: root}}
    scopes_dir = tmp_path / "config" / "scopes"
    scopes_dir.mkdir(parents=True, exist_ok=True)
    (scopes_dir / "bot.yml").write_text(
        yaml.safe_dump({"workspace": {"name": "w", "pools": pools}}, sort_keys=False),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_delete_unreferenced_returns_200_and_removes_file(tmp_path: Path) -> None:
    """DELETE on a prompt no pool references returns 200 and removes the md."""
    md = _seed_md(tmp_path, "orphan", "nobody uses me")
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/prompts/orphan")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data == {"deleted": "orphan"}
        assert not md.exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_referenced_by_main_returns_409(tmp_path: Path) -> None:
    """DELETE on a prompt whose main agent explicitly references it returns 409."""
    _seed_md(tmp_path, "shared", "body")
    _seed_pool_yml(tmp_path, "default", main_prompt_name="shared")
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/prompts/shared")
        assert resp.status == 409, await resp.text()
        data = await resp.json()
        assert data["error"] == "in_use"
        assert len(data["usages"]) == 1
        usage = data["usages"][0]
        assert usage["pool"] == "default"
        assert usage["agent_kind"] == "main"
        assert usage["agent_name"] == "default"
        # File was NOT removed.
        assert (tmp_path / "agents" / "shared.md").exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_referenced_by_subagent_returns_409(tmp_path: Path) -> None:
    """DELETE on a prompt a subagent explicitly references returns 409."""
    _seed_md(tmp_path, "subprompt", "body")
    _seed_pool_yml(
        tmp_path,
        "coder",
        subagents=[{"agent_name": "worker", "prompt_name": "subprompt"}],
    )
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/prompts/subprompt")
        assert resp.status == 409, await resp.text()
        data = await resp.json()
        assert data["error"] == "in_use"
        assert len(data["usages"]) == 1
        usage = data["usages"][0]
        assert usage["pool"] == "coder"
        assert usage["agent_kind"] == "subagent"
        assert usage["agent_name"] == "worker"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_fallback_reference_returns_409(tmp_path: Path) -> None:
    """DELETE on a prompt whose main agent has empty prompt_name but matching
    agent_name returns 409 (the backward-compat fallback case)."""
    _seed_md(tmp_path, "main", "body")
    # No prompt_name set → main agent falls back to agents/<agent_name>.md.
    _seed_pool_yml(tmp_path, "main", main_agent_name="main")
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/prompts/main")
        assert resp.status == 409, await resp.text()
        data = await resp.json()
        assert data["error"] == "in_use"
        assert len(data["usages"]) == 1
        usage = data["usages"][0]
        assert usage["pool"] == "main"
        assert usage["agent_kind"] == "main"
        assert usage["agent_name"] == "main"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_multi_pool_reference_returns_409_with_all_usages(
    tmp_path: Path,
) -> None:
    """DELETE on a prompt referenced by multiple pools returns 409 with every usage."""
    _seed_md(tmp_path, "common", "body")
    # Pool A: main references explicitly.
    _seed_pool_yml(tmp_path, "pool-a", main_prompt_name="common")
    # Pool B: subagent references explicitly.
    _seed_pool_yml(
        tmp_path,
        "pool-b",
        subagents=[{"agent_name": "helper", "prompt_name": "common"}],
    )
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/prompts/common")
        assert resp.status == 409, await resp.text()
        data = await resp.json()
        assert data["error"] == "in_use"
        assert len(data["usages"]) == 2
        pools = {u["pool"] for u in data["usages"]}
        assert pools == {"pool-a", "pool-b"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_404_when_missing(tmp_path: Path) -> None:
    """DELETE on a prompt that doesn't exist returns 404, not 409."""
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/prompts/ghost")
        assert resp.status == 404, await resp.text()
        data = await resp.json()
        assert "unknown prompt" in data["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_rejects_invalid_name_400(tmp_path: Path) -> None:
    """Names not matching the agent-name regex are rejected with 400."""
    controller = _make_controller(tmp_path)
    client = _make_client(controller, tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/prompts/BadName")
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "validation"
        assert "name" in data["fields"]
    finally:
        await client.close()
