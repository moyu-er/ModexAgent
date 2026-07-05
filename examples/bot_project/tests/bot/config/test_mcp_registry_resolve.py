"""Tests for bot.config.mcp_registry (Task 1.7)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.config.mcp_registry import (
    REGISTRY_PATH,
    UnknownMcpServer,
    read_registry,
    resolve_agent_mcp_servers,
)


def _write_registry(tmp_path: Path, servers: dict) -> Path:
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return p


class TestReadRegistry:
    def test_returns_servers_mapping(self, tmp_path: Path) -> None:
        p = _write_registry(tmp_path, {"playwright": {"command": "npx"}})
        reg = read_registry(p)
        assert "playwright" in reg
        assert reg["playwright"]["command"] == "npx"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_registry(tmp_path / "nope.json") == {}

    def test_accepts_servers_alias(self, tmp_path: Path) -> None:
        p = tmp_path / "registry.json"
        p.write_text(json.dumps({"servers": {"fetch": {"url": "x"}}}), encoding="utf-8")
        reg = read_registry(p)
        assert "fetch" in reg


class TestResolveAgentMcpServers:
    def test_resolves_selection(self, tmp_path: Path) -> None:
        p = _write_registry(
            tmp_path,
            {"playwright": {"command": "npx"}, "fetch": {"url": "x"}, "MiniMax": {"command": "uvx"}},
        )
        resolved = resolve_agent_mcp_servers(["playwright", "fetch"], p)
        assert set(resolved.keys()) == {"playwright", "fetch"}
        assert resolved["playwright"]["command"] == "npx"

    def test_unknown_raises(self, tmp_path: Path) -> None:
        p = _write_registry(tmp_path, {"playwright": {"command": "npx"}})
        with pytest.raises(UnknownMcpServer):
            resolve_agent_mcp_servers(["playwright", "nope"], p)

    def test_empty_selection_returns_empty(self, tmp_path: Path) -> None:
        p = _write_registry(tmp_path, {"playwright": {"command": "npx"}})
        assert resolve_agent_mcp_servers([], p) == {}


class TestRegistryPathConstant:
    def test_registry_path_is_config_mcp_registry_json(self) -> None:
        # Convention: config/mcp/registry.json (portable Path, not absolute).
        assert REGISTRY_PATH.as_posix().endswith("config/mcp/registry.json")
