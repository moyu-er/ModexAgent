"""Tests for ``modex_agent.ioc.configs.mcp``.

``MCPServerEntry`` is the single authority on the MCP server wire shape —
reused by the bot-layer registry CRUD API, so it must:

* accept every transport spelling the load path (``MCPClientManager``) accepts
  (``streamable_http`` is the form used in the shipped ``registry.json`` and
  the Claude ``mcp.json`` convention; ``http``/``local`` are legacy aliases);
* round-trip the ``type`` <-> ``transport`` alias and accept ``environment``
  as an input alias for ``env``;
* be a rule-12 value object (``frozen=True``, ``extra="forbid"``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.ioc.configs.mcp import MCPConfig, MCPServerEntry


class TestTransportNormalization:
    def test_streamable_http_underscore_accepted(self) -> None:
        """registry.json ships with ``streamable_http`` (underscore)."""
        e = MCPServerEntry.model_validate(
            {"type": "streamable_http", "url": "https://x/mcp"}
        )
        assert e.transport == "streamableHttp"

    def test_streamable_http_dash_and_compact_accepted(self) -> None:
        assert MCPServerEntry.model_validate({"type": "streamable-http"}).transport == "streamableHttp"
        assert MCPServerEntry.model_validate({"type": "streamablehttp"}).transport == "streamableHttp"

    def test_http_alias_accepted(self) -> None:
        assert MCPServerEntry.model_validate({"type": "http"}).transport == "streamableHttp"

    def test_local_alias_maps_to_stdio(self) -> None:
        assert MCPServerEntry.model_validate({"type": "local", "command": "npx"}).transport == "stdio"

    def test_canonical_literals_pass_unchanged(self) -> None:
        assert MCPServerEntry.model_validate({"type": "stdio"}).transport == "stdio"
        assert MCPServerEntry.model_validate({"type": "sse"}).transport == "sse"
        assert MCPServerEntry.model_validate({"type": "streamableHttp"}).transport == "streamableHttp"

    def test_invalid_transport_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MCPServerEntry.model_validate({"type": "websocket"})


class TestAliasesAndRoundTrip:
    def test_type_alias_round_trip(self) -> None:
        e = MCPServerEntry.model_validate({"type": "stdio", "command": "npx"})
        assert e.transport == "stdio"
        dumped = e.model_dump(by_alias=True, exclude_none=True)
        assert dumped["type"] == "stdio"
        assert dumped["command"] == "npx"
        assert "transport" not in dumped

    def test_environment_input_alias_serializes_as_env(self) -> None:
        """``environment`` is accepted on INPUT; output is the field name ``env``."""
        e = MCPServerEntry.model_validate({"environment": {"K": "v"}})
        assert e.env == {"K": "v"}
        # The framework model has no output alias — it serializes as ``env``,
        # which matches the shipped registry.json key (fixes the earlier
        # silent env->environment rename on write).
        assert e.model_dump(by_alias=True)["env"] == {"K": "v"}

    def test_command_list_split_into_command_and_args(self) -> None:
        e = MCPServerEntry.model_validate({"command": ["npx", "-y", "fs"]})
        assert e.command == "npx"
        assert e.args == ["-y", "fs"]


class TestValueObjectSemantics:
    def test_defaults(self) -> None:
        e = MCPServerEntry()
        assert e.transport is None
        assert e.command == ""
        assert e.args == []
        assert e.env == {}
        assert e.timeout == 30

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MCPServerEntry.model_validate({"command": "x", "bogus": 1})

    def test_frozen(self) -> None:
        e = MCPServerEntry(command="x")
        with pytest.raises(ValidationError):
            e.command = "y"  # type: ignore[misc]


class TestMCPConfig:
    def test_defaults(self) -> None:
        c = MCPConfig()
        assert c.enabled is True
        c.servers["x"] = MCPServerEntry(command="npx")
        assert "x" in c.servers
