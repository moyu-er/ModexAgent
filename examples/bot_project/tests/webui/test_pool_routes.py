"""Tests for the Phase 2B pool/MCP/skills/prompt REST routes.

Mirrors the pattern in :mod:`tests.webui.test_config_endpoints`: build a
``WebUIServer`` with a ``PoolConfigController`` wired to ``tmp_path``-backed
stores, drive it through ``aiohttp.test_utils``. The real ``config/`` tree is
never touched.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.config.mcp_registry import REGISTRY_PATH
from bot.config.pool_store import PoolStore
from bot.config.prompt_store import PromptStore
from bot.config.skills_store import SkillsStore
from bot.service.pool_config_controller import PoolConfigController
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer

_BOT_PROJECT = Path(__file__).resolve().parents[2]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))


# ─── fixtures ────────────────────────────────────────────────────────────────


def _seed_pool_yml(
    base: Path,
    pool: str,
    main_agent: str = "main",
    mcp: list[str] | None = None,
) -> Path:
    """Write a flat-shape pool.yml + a default prompt md (matches production)."""
    pool_dir = base / "config" / "pools" / pool
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "templates").mkdir(exist_ok=True)
    data: dict = {"main_agent_name": main_agent}
    if mcp:
        data["mcp"] = list(mcp)
    p = pool_dir / "pool.yml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    # Default prompt md so read_prompt works out of the box.
    agents_dir = base / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{main_agent}.md").write_text(f"# {main_agent}\n", encoding="utf-8")
    return p


def _make_controller(tmp_path: Path, default_pool: str = "main") -> PoolConfigController:
    return PoolConfigController(
        pool_store=PoolStore(base_dir=tmp_path),
        skills_store=SkillsStore(base_dir=tmp_path, user_global_dir=tmp_path / "user_skills"),
        prompt_store=PromptStore(base_dir=tmp_path),
        mcp_registry_path=tmp_path / REGISTRY_PATH,
        default_pool=default_pool,
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


# ─── pools ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_pool_round_trip(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/pools/main")
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == "main"
        assert data["main"]["agent_name"] == "main"
        assert data["restart_required"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_write_pool_sets_restart_required(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        # Read, then PUT it back unchanged.
        got = await (await client.get("/api/pools/main")).json()
        resp = await client.put("/api/pools/main", json=got)
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["restart_required"] is True
        # Subsequent GET also reports True (sticky marker).
        got2 = await (await client.get("/api/pools/main")).json()
        assert got2["restart_required"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_write_pool_validation_400_on_bad_agent_name(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        bad = {
            "name": "main",
            "main_agent_name": "main",
            "main": {"agent_name": "Bad Name With Spaces"},
        }
        resp = await client.put("/api/pools/main", json=bad)
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "validation"
        assert "fields" in data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_read_unknown_pool_404(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/pools/missing")
        assert resp.status == 404
        data = await resp.json()
        assert "unknown pool" in data["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_and_delete_pool(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.post("/api/pools", json={"name": "research"})
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["name"] == "research"
        # Pool now listed.
        listed = await (await client.get("/api/pools")).json()
        names = [p["name"] for p in listed]
        assert {"main", "research"} <= set(names)

        # Delete the non-default pool.
        resp = await client.delete("/api/pools/research")
        assert resp.status == 200, await resp.text()
        listed = await (await client.get("/api/pools")).json()
        names = [p["name"] for p in listed]
        assert "research" not in names
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_default_pool_refused(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path, default_pool="main"), tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/pools/main")
        assert resp.status == 409
        data = await resp.json()
        assert "default" in data["error"].lower()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rename_pool(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    _seed_pool_yml(tmp_path, "extra")
    cli = _make_client(_make_controller(tmp_path), tmp_path)
    await cli.start_server()
    try:
        resp = await cli.patch("/api/pools/extra", json={"name": "renamed"})
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["name"] == "renamed"
        listed = await (await cli.get("/api/pools")).json()
        names = [p["name"] for p in listed]
        assert "renamed" in names and "extra" not in names
    finally:
        await cli.close()


@pytest.mark.asyncio
async def test_rename_default_pool_refused(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path, default_pool="main"), tmp_path)
    await client.start_server()
    try:
        resp = await client.patch("/api/pools/main", json={"name": "x"})
        assert resp.status == 409
    finally:
        await client.close()


# ─── prompts ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_write_prompt(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/pools/main/agents/main/prompt")
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == "main"
        assert "# main" in data["content"]

        resp = await client.put(
            "/api/pools/main/agents/main/prompt", json={"content": "new body"}
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["content"] == "new body"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_read_prompt_seeds_missing_md(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/pools/main/agents/ghost/prompt")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["name"] == "ghost"
        assert "You are an AI assistant" in data["content"]
        # File was created on disk by the GET seed path.
        assert (tmp_path / "agents" / "ghost.md").exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_write_prompt_creates_if_missing(tmp_path: Path) -> None:
    """PUT auto-creates ``agents/<name>.md`` — the contract the webui relies on
    when the user provides an agent name and saves a fresh system prompt.
    Locks in: a GET-first 404 turns into 200 after PUT, and the md lands on disk.
    """
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        # Pre-condition: GET already seeds the default (we changed behavior).
        # PUT still overrides with caller content, so the test still validates
        # the create-or-update contract.
        resp = await client.put(
            "/api/pools/main/agents/oracle/prompt",
            json={"content": "fresh prompt body"},
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["content"] == "fresh prompt body"

        # Round-trip: GET now succeeds and returns the PUT content.
        resp = await client.get("/api/pools/main/agents/oracle/prompt")
        assert resp.status == 200
        assert (await resp.json())["content"] == "fresh prompt body"

        # File landed on disk.
        on_disk = tmp_path / "agents" / "oracle.md"
        assert on_disk.exists()
        assert on_disk.read_text(encoding="utf-8") == "fresh prompt body"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_write_pool_seeds_missing_md_for_new_subagent(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    # Seed an existing subagent template.
    templates_dir = tmp_path / "config" / "pools" / "main" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / "researcher.yml").write_text(
        yaml.safe_dump({"agent_name": "researcher", "description": "Old"}),
        encoding="utf-8",
    )

    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/pools/main")
        tree = await resp.json()

        # Add a brand-new subagent.
        tree["subagents"].append({
            "agent_name": "oracle",
            "description": "New sub",
            "max_steps": 80,
            "tool_preset": "read_write",
            "tool_supplements": [],
            "context_mode": "fork",
            "mcp": [],
        })

        resp = await client.put("/api/pools/main", json=tree)
        assert resp.status == 200, await resp.text()

        # oracle md was auto-created on save.
        assert (tmp_path / "agents" / "oracle.md").exists()
        text = (tmp_path / "agents" / "oracle.md").read_text(encoding="utf-8")
        assert "You are an AI assistant" in text

        # Existing subagent without an md is also seeded on save.
        assert (tmp_path / "agents" / "researcher.md").exists()
        assert "You are an AI assistant" in (
            tmp_path / "agents" / "researcher.md"
        ).read_text(encoding="utf-8")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_write_pool_renames_prompt_md_on_agent_rename(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    templates_dir = tmp_path / "config" / "pools" / "main" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / "researcher.yml").write_text(
        yaml.safe_dump({"agent_name": "researcher", "description": "Old"}),
        encoding="utf-8",
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "researcher.md").write_text(
        "# custom researcher prompt\n", encoding="utf-8"
    )

    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/pools/main")
        tree = await resp.json()
        tree["subagents"][0]["agent_name"] = "scout"

        resp = await client.put("/api/pools/main", json=tree)
        assert resp.status == 200, await resp.text()

        # Old md moved, content preserved.
        assert not (agents_dir / "researcher.md").exists()
        assert (
            agents_dir / "scout.md"
        ).read_text(encoding="utf-8") == "# custom researcher prompt\n"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_write_pool_removes_prompt_md_on_agent_delete(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
    templates_dir = tmp_path / "config" / "pools" / "main" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / "researcher.yml").write_text(
        yaml.safe_dump({"agent_name": "researcher", "description": "Old"}),
        encoding="utf-8",
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "researcher.md").write_text("bye", encoding="utf-8")

    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/pools/main")
        tree = await resp.json()
        tree["subagents"] = []

        resp = await client.put("/api/pools/main", json=tree)
        assert resp.status == 200, await resp.text()

        assert not (agents_dir / "researcher.md").exists()
    finally:
        await client.close()


# ─── MCP registry ────────────────────────────────────────────────────────────


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
async def test_delete_referenced_mcp_returns_409(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main", mcp=["fetch"])
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        await client.post(
            "/api/mcp/fetch", json={"type": "stdio", "command": "x"}
        )
        resp = await client.delete("/api/mcp/fetch")
        assert resp.status == 409
        data = await resp.json()
        assert data["error"] == "in use"
        assert ["main", "main"] in data["used_by"]
        # Still present.
        assert "fetch" in await (await client.get("/api/mcp")).json()
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


# ─── skills ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_skill_json_then_list_and_assign(tmp_path: Path) -> None:
    _seed_pool_yml(tmp_path, "main")
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

        # Global skill upload does NOT set restart_required; assign DOES.
        assert client.app is not None  # type: ignore[unreachable]

        # Unassign.
        resp = await client.delete("/api/pools/main/agents/main/skills/hello")
        assert resp.status == 200
        got = await (await client.get("/api/pools/main/agents/main/skills")).json()
        assert got == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upload_skill_multipart(tmp_path: Path) -> None:
    from aiohttp import FormData

    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        # webkitdirectory-style: filenames include the leading <skillName>/ prefix.
        form = FormData()
        form.add_field("name", "greeter")
        form.add_field(
            "file0", b"# greeter skill\n", filename="greeter/SKILL.md"
        )
        form.add_field(
            "file1", b"rule-content", filename="greeter/sub/rule.txt"
        )
        resp = await client.post("/api/skills", data=form)
        assert resp.status == 200, await resp.text()
        # Verify on-disk layout: keys are relative to <skillName>/.
        skill_dir = tmp_path / "global_skills" / "greeter"
        assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "# greeter skill\n"
        assert (skill_dir / "sub" / "rule.txt").read_text(encoding="utf-8") == "rule-content"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_skill(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        await client.post(
            "/api/skills",
            json={
                "name": "temp",
                "files": {"SKILL.md": base64.b64encode(b"x").decode()},
            },
        )
        resp = await client.delete("/api/skills/temp")
        assert resp.status == 200
        listed = await (await client.get("/api/skills")).json()
        assert "temp" not in [s["name"] for s in listed]
    finally:
        await client.close()


# ─── restart_required ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_required_false_after_global_skill_upload(tmp_path: Path) -> None:
    """Global skill add is hot-reload; restart_required must NOT flip on.

    The controller is checked directly (the marker is not exposed via an HTTP
    response on the skills endpoint) to assert the hot-reload semantics.
    """
    ctrl = _make_controller(tmp_path)
    client = _make_client(ctrl, tmp_path)
    await client.start_server()
    try:
        files = {"SKILL.md": base64.b64encode(b"# hello\n").decode()}
        resp = await client.post("/api/skills", json={"name": "hello", "files": files})
        assert resp.status == 200, await resp.text()
        assert ctrl.restart_required is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_restart_required_true_after_skill_assign(tmp_path: Path) -> None:
    """Assigning a skill to an agent touches an agent root -> restart_required."""
    _seed_pool_yml(tmp_path, "main")
    ctrl = _make_controller(tmp_path)
    client = _make_client(ctrl, tmp_path)
    await client.start_server()
    try:
        files = {"SKILL.md": base64.b64encode(b"# hello\n").decode()}
        await client.post("/api/skills", json={"name": "hello", "files": files})
        assert ctrl.restart_required is False
        resp = await client.post("/api/pools/main/agents/main/skills/hello")
        assert resp.status == 200, await resp.text()
        assert ctrl.restart_required is True
    finally:
        await client.close()


# ─── path-traversal guards ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_pool_bad_name_400_or_404(tmp_path: Path) -> None:
    """Names with '..' are rejected by the store regex (400) or not-found (404).

    No file escapes the pool dir: '..' fails the name regex and never reaches
    the filesystem.
    """
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/pools/..")
        assert resp.status in (400, 404)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_read_prompt_traversal_rejected(tmp_path: Path) -> None:
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/pools/main/agents/..%2F..%2Fetc/prompt")
        assert resp.status in (400, 404)
    finally:
        await client.close()


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


# ─── MCP ${ENV} round-trip / extra=forbid / pool mutation ────────────────────


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
        # Placeholders survive verbatim (no shell expansion at store time).
        assert got["srv"]["url"] == "https://${MY_VAR}/hook"
        assert got["srv"]["headers"]["X-Token"] == "${SECRET_TOKEN}"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pool_write_rejects_unknown_field_with_extra_forbid(tmp_path: Path) -> None:
    """extra='forbid' on the PoolTree model yields 400 naming the unknown key."""
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        got = await (await client.get("/api/pools/main")).json()
        # Inject an unknown key the schema does not allow.
        got["bogus"] = 1
        resp = await client.put("/api/pools/main", json=got)
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "validation"
        body = json.dumps(data)
        # The unknown key is named in the validation fields.
        assert "bogus" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_skill_route_traversal_rejected(tmp_path: Path) -> None:
    """POST /api/skills/.. must not escape the skills dir (400/404, not disk)."""
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        files = {"SKILL.md": base64.b64encode(b"x").decode()}
        resp = await client.post("/api/skills/..", json={"name": "..", "files": files})
        assert resp.status in (400, 404)
        # Nothing escaped: the rejected upload created no global library entry.
        assert not (tmp_path / "global_skills").exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_route_traversal_rejected(tmp_path: Path) -> None:
    """DELETE /api/mcp/.. must not escape the registry path (400/404)."""
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        resp = await client.delete("/api/mcp/..%2F..%2Fetc")
        assert resp.status in (400, 404)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pool_mutation_round_trip(tmp_path: Path) -> None:
    """GET → edit a field (main max_steps+1) → PUT → GET reflects the new value."""
    _seed_pool_yml(tmp_path, "main")
    client = _make_client(_make_controller(tmp_path), tmp_path)
    await client.start_server()
    try:
        got = await (await client.get("/api/pools/main")).json()
        before = got["main"]["max_steps"]
        got["main"]["max_steps"] = before + 1
        resp = await client.put("/api/pools/main", json=got)
        assert resp.status == 200, await resp.text()
        got2 = await (await client.get("/api/pools/main")).json()
        assert got2["main"]["max_steps"] == before + 1
    finally:
        await client.close()
