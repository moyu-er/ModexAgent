"""Tests for the Phase 2A write operations in bot.config.mcp_registry.

Kept separate from ``test_mcp_registry_resolve.py`` (the read/resolve tests)
so each file has a single concern. All tests use ``tmp_path``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.config.mcp_registry import (
    delete_server,
    read_registry,
    server_used_by,
    upsert_server,
    write_registry,
)

from modex_agent.ioc.configs.mcp import MCPServerEntry


def _reg_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.json"


# ─── write_registry ──────────────────────────────────────────────────────────


class TestWriteRegistry:
    def test_writes_mcp_servers_shape(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        write_registry({"playwright": MCPServerEntry(command="npx", args=["@playwright/mcp"])}, p)
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert "mcpServers" in raw
        assert raw["mcpServers"]["playwright"]["command"] == "npx"

    def test_accepts_raw_dict_entry(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        write_registry({"x": {"command": "npx"}}, p)
        assert read_registry(p)["x"]["command"] == "npx"

    def test_transport_written_as_type_alias(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        write_registry({"fetch": MCPServerEntry(transport="streamableHttp", url="u")}, p)
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert raw["mcpServers"]["fetch"]["type"] == "streamableHttp"
        assert "transport" not in raw["mcpServers"]["fetch"]

    def test_empty_containers_excluded(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        write_registry({"x": MCPServerEntry(command="npx")}, p)
        raw = json.loads(p.read_text(encoding="utf-8"))["mcpServers"]["x"]
        assert "args" not in raw  # empty list excluded
        assert "env" not in raw  # empty dict excluded (alias environment)

    def test_env_round_trips_as_env_key(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        write_registry(
            {"m": MCPServerEntry(command="uvx", env={"K": "${V}"})}, p
        )
        raw = json.loads(p.read_text(encoding="utf-8"))["mcpServers"]["m"]
        # Framework model serializes ``env`` as the field name ``env`` (it
        # accepts ``environment`` on INPUT only). This matches the shipped
        # registry.json key and avoids the silent env->environment rename.
        assert raw["env"] == {"K": "${V}"}
        # And re-read preserves the ${ENV} placeholder (no interpolation here).
        re_entry = MCPServerEntry.model_validate(raw)
        assert re_entry.env == {"K": "${V}"}

    def test_no_tmp_left_behind(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        write_registry({"x": MCPServerEntry(command="npx")}, p)
        assert not (tmp_path / "registry.json.tmp").exists()

    def test_closed_loop_round_trip(self, tmp_path: Path) -> None:
        """Write a real MCPServerEntry, read it back, re-validate, and assert
        value equality. On-disk wire form: ``type`` (transport alias) and
        ``env`` (field name; ``environment`` is an input-only alias)."""
        p = _reg_path(tmp_path)
        original = MCPServerEntry(
            transport="streamableHttp",
            command="uvx",
            args=["mcp-server-fetch"],
            env={"HTTP_TIMEOUT": "30", "API_KEY": "${MY_KEY}"},
            url="https://example.invalid/mcp",
            headers={"Authorization": "Bearer ${TOKEN}"},
            timeout=45,
        )
        write_registry({"fetch": original}, p)
        reg = read_registry(p)
        assert "fetch" in reg
        restored = MCPServerEntry.model_validate(reg["fetch"])
        assert restored == original
        # Belt-and-braces: wire form actually exercised on disk.
        raw = json.loads(p.read_text(encoding="utf-8"))["mcpServers"]["fetch"]
        assert raw["type"] == "streamableHttp"
        assert raw["env"] == {
            "HTTP_TIMEOUT": "30",
            "API_KEY": "${MY_KEY}",
        }
        assert "transport" not in raw
        assert "environment" not in raw


# ─── upsert / delete ─────────────────────────────────────────────────────────


class TestUpsertDelete:
    def test_upsert_inserts(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        write_registry({"a": MCPServerEntry(command="x")}, p)
        upsert_server("b", MCPServerEntry(command="y"), p)
        reg = read_registry(p)
        assert set(reg.keys()) == {"a", "b"}

    def test_upsert_updates_in_place(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        write_registry({"a": MCPServerEntry(command="x")}, p)
        upsert_server("a", MCPServerEntry(command="changed"), p)
        assert read_registry(p)["a"]["command"] == "changed"

    def test_upsert_round_trips_through_read(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        upsert_server("pw", MCPServerEntry(command="npx", args=["@playwright/mcp"]), p)
        reg = read_registry(p)
        assert reg["pw"]["args"] == ["@playwright/mcp"]

    def test_delete_removes(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        write_registry({"a": MCPServerEntry(command="x"), "b": MCPServerEntry(command="y")}, p)
        assert delete_server("a", p) is True
        reg = read_registry(p)
        assert "a" not in reg
        assert "b" in reg

    def test_delete_missing_returns_false(self, tmp_path: Path) -> None:
        p = _reg_path(tmp_path)
        write_registry({"a": MCPServerEntry(command="x")}, p)
        assert delete_server("nope", p) is False
        # Registry unchanged.
        assert "a" in read_registry(p)

    def test_delete_referenced_server_still_removes_it(
        self, tmp_path: Path
    ) -> None:
        # The store does NOT refuse deletion — callers decide via server_used_by.
        p = _reg_path(tmp_path)
        write_registry({"a": MCPServerEntry(command="x")}, p)
        assert delete_server("a", p) is True
        assert "a" not in read_registry(p)


# ─── server_used_by ──────────────────────────────────────────────────────────


def _seed_pool_with_mcp(
    base: Path,
    pool: str,
    main_agent: str,
    main_mcp: list[str],
    subagents: dict[str, list[str]] | None = None,
) -> None:
    """Seed a pool with the FLAT pool.yml shape (top-level main-agent mcp list)."""
    pool_dir = base / "config" / "pools" / pool
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "templates").mkdir(exist_ok=True)
    import yaml

    (pool_dir / "pool.yml").write_text(
        yaml.safe_dump(
            {"main_agent_name": main_agent, "mcp": main_mcp},
        ),
        encoding="utf-8",
    )
    for sub_name, sub_mcp in (subagents or {}).items():
        (pool_dir / "templates" / f"{sub_name}.yml").write_text(
            yaml.safe_dump({"agent_name": sub_name, "mcp": sub_mcp}),
            encoding="utf-8",
        )


def _seed_pool_with_legacy_agents_mcp(
    base: Path, pool: str, main_agent: str, main_mcp: list[str]
) -> None:
    """Seed the LEGACY `agents:` block shape (pre-flat schema) — fallback path."""
    pool_dir = base / "config" / "pools" / pool
    pool_dir.mkdir(parents=True, exist_ok=True)
    import yaml

    (pool_dir / "pool.yml").write_text(
        yaml.safe_dump(
            {"agents": [{"name": main_agent, "role": "main", "mcp": main_mcp}]},
        ),
        encoding="utf-8",
    )


class TestServerUsedBy:
    def test_finds_main_agent_reference_flat(self, tmp_path: Path) -> None:
        # Flat pool.yml: top-level mcp list belongs to the main agent.
        _seed_pool_with_mcp(tmp_path, "main", "main", ["playwright", "fetch"])
        used = server_used_by("playwright", tmp_path / "config" / "pools")
        assert ("main", "main") in used

    def test_finds_main_agent_reference_legacy_agents_block(self, tmp_path: Path) -> None:
        # Legacy `agents:` shape is still honored as a fallback.
        _seed_pool_with_legacy_agents_mcp(tmp_path, "main", "main", ["playwright"])
        used = server_used_by("playwright", tmp_path / "config" / "pools")
        assert ("main", "main") in used

    def test_main_agent_name_defaults_to_pool_dir(self, tmp_path: Path) -> None:
        # No main_agent_name → attribute to the pool/dir name.
        pool_dir = tmp_path / "config" / "pools" / "default"
        pool_dir.mkdir(parents=True)
        import yaml

        (pool_dir / "pool.yml").write_text(
            yaml.safe_dump({"mcp": ["playwright"]}), encoding="utf-8"
        )
        used = server_used_by("playwright", tmp_path / "config" / "pools")
        assert ("default", "default") in used

    def test_finds_subagent_reference(self, tmp_path: Path) -> None:
        _seed_pool_with_mcp(
            tmp_path,
            "coding",
            "coding",
            [],
            {"scout": ["playwright"], "worker": []},
        )
        used = server_used_by("playwright", tmp_path / "config" / "pools")
        assert ("coding", "scout") in used
        assert ("coding", "worker") not in used

    def test_no_references_returns_empty(self, tmp_path: Path) -> None:
        _seed_pool_with_mcp(tmp_path, "main", "main", ["other"])
        assert server_used_by("playwright", tmp_path / "config" / "pools") == []

    def test_missing_pools_dir_returns_empty(self, tmp_path: Path) -> None:
        assert server_used_by("x", tmp_path / "nope") == []
