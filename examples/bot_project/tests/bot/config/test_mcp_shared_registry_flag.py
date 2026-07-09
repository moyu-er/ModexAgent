"""Tests for ``read_shared_registry_flag`` (ADR-0017 Task 5a).

The flag gates whether the bot opts into the shared MCP connection registry
for the MAIN-AGENT path. It defaults to ON (``True``) and fails open on every
degenerate input (missing file, malformed JSON, non-dict root, absent key) so
that a corrupted/missing registry config can never break MCP — the worst case
is falling back to today's per-pool ``MCPClientManager`` path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.config.mcp_registry import read_shared_registry_flag


class TestReadSharedRegistryFlag:
    def test_missing_file_returns_true(self, tmp_path: Path) -> None:
        # No registry.json at all → flag defaults on.
        assert read_shared_registry_flag(tmp_path / "nope.json") is True

    def test_absent_key_returns_true(self, tmp_path: Path) -> None:
        p = tmp_path / "registry.json"
        p.write_text(json.dumps({"mcpServers": {"s1": {"command": "echo"}}}), "utf-8")
        # sharedRegistry key absent → default on.
        assert read_shared_registry_flag(p) is True

    def test_explicit_true_returns_true(self, tmp_path: Path) -> None:
        p = tmp_path / "registry.json"
        p.write_text(json.dumps({"mcpServers": {}, "sharedRegistry": True}), "utf-8")
        assert read_shared_registry_flag(p) is True

    def test_explicit_false_returns_false(self, tmp_path: Path) -> None:
        p = tmp_path / "registry.json"
        p.write_text(json.dumps({"mcpServers": {}, "sharedRegistry": False}), "utf-8")
        assert read_shared_registry_flag(p) is False

    def test_malformed_json_returns_true(self, tmp_path: Path) -> None:
        # Fail-open: a corrupted file must not crash startup; the bot falls
        # back to the per-pool path.
        p = tmp_path / "registry.json"
        p.write_text("{not valid json", "utf-8")
        assert read_shared_registry_flag(p) is True

    def test_non_dict_root_returns_true(self, tmp_path: Path) -> None:
        # JSON top-level is a list/number/string → treat as no flag, default on.
        p = tmp_path / "registry.json"
        p.write_text(json.dumps(["just", "a", "list"]), "utf-8")
        assert read_shared_registry_flag(p) is True

    def test_string_value_coerced_to_bool(self, tmp_path: Path) -> None:
        # bool("false") is True in Python, but the contract is bool(value):
        # a present truthy string is on. This pins the literal ``bool(value)``
        # semantics rather than string parsing (we don't try to be clever).
        p = tmp_path / "registry.json"
        p.write_text(json.dumps({"sharedRegistry": "false"}), "utf-8")
        assert read_shared_registry_flag(p) is True  # non-empty string → True

    def test_default_path_argument(self, tmp_path: Path, monkeypatch) -> None:
        # When called with no arg, it reads REGISTRY_PATH. Point that constant
        # at a tmp file so the test is hermetic and doesn't depend on the
        # checked-in config/mcp/registry.json.
        import bot.config.mcp_registry as mod

        p = tmp_path / "registry.json"
        p.write_text(json.dumps({"sharedRegistry": False}), "utf-8")
        monkeypatch.setattr(mod, "REGISTRY_PATH", p)
        assert read_shared_registry_flag() is False
