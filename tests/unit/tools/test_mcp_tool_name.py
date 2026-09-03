"""Unit tests for MCP tool name sanitization.

Locks the contract of ``_sanitize_name`` and ``_mcp_tool_name`` — the
opencode-aligned naming convention where tool identifiers are built as
``{sanitize(server)}_{sanitize(tool)}`` with any character outside
``[a-zA-Z0-9_-]`` replaced by underscore.
"""

from __future__ import annotations

from modex_agent.tools.mcp.tool import _mcp_tool_name, _sanitize_name

# ── _sanitize_name ───────────────────────────────────────────────────────────


class TestSanitizeName:
    """``_sanitize_name`` replaces non-[a-zA-Z0-9_-] chars with ``_``."""

    def test_alphanumeric_unchanged(self) -> None:
        assert _sanitize_name("playwright") == "playwright"

    def test_underscore_unchanged(self) -> None:
        assert _sanitize_name("my_server") == "my_server"

    def test_hyphen_unchanged(self) -> None:
        assert _sanitize_name("my-server") == "my-server"

    def test_digits_unchanged(self) -> None:
        assert _sanitize_name("server123") == "server123"

    def test_dot_replaced(self) -> None:
        assert _sanitize_name("my.server") == "my_server"

    def test_colon_replaced(self) -> None:
        assert _sanitize_name("server:name") == "server_name"

    def test_slash_replaced(self) -> None:
        assert _sanitize_name("server/name") == "server_name"

    def test_space_replaced(self) -> None:
        assert _sanitize_name("my server") == "my_server"

    def test_multiple_special_chars(self) -> None:
        assert _sanitize_name("my.server:test/foo") == "my_server_test_foo"

    def test_empty_string(self) -> None:
        assert _sanitize_name("") == ""

    def test_all_special_chars(self) -> None:
        assert _sanitize_name("...///:::") == "_________"

    def test_unicode_replaced(self) -> None:
        assert _sanitize_name("sérver") == "s_rver"

    def test_mixed_valid_invalid(self) -> None:
        assert _sanitize_name("a.b-c_d") == "a.b-c_d".replace(".", "_")


# ── _mcp_tool_name ───────────────────────────────────────────────────────────


class TestMcpToolName:
    """``_mcp_tool_name`` builds ``{sanitize(server)}_{sanitize(tool)}``."""

    def test_basic(self) -> None:
        assert _mcp_tool_name("playwright", "navigate") == "playwright_navigate"

    def test_server_with_dot(self) -> None:
        assert _mcp_tool_name("my.server", "tool") == "my_server_tool"

    def test_tool_with_dot(self) -> None:
        assert _mcp_tool_name("server", "tool.name") == "server_tool_name"

    def test_both_sanitized(self) -> None:
        assert _mcp_tool_name("my-server.test", "tool.name") == "my-server_test_tool_name"

    def test_colon_in_neither_part(self) -> None:
        """Colon must NOT appear in the output (LLM tool-name regex forbids it)."""
        name = _mcp_tool_name("server:name", "tool:action")
        assert ":" not in name
        assert name == "server_name_tool_action"

    def test_separator_is_underscore(self) -> None:
        """The separator between server and tool is exactly one ``_``."""
        name = _mcp_tool_name("srv", "act")
        assert name.count("_") == 1

    def test_empty_server(self) -> None:
        assert _mcp_tool_name("", "tool") == "_tool"

    def test_empty_tool(self) -> None:
        assert _mcp_tool_name("server", "") == "server_"

    def test_matches_opencode_convention(self) -> None:
        """Verify byte-for-byte parity with opencode's sanitize + toolName.

        opencode: ``sanitize(clientName) + "_" + sanitize(name)``
        where ``sanitize = (v) => v.replace(/[^a-zA-Z0-9_-]/g, "_")``
        """
        # These are the exact examples from the opencode docs
        assert _mcp_tool_name("playwright", "browser_navigate") == "playwright_browser_navigate"
        assert _mcp_tool_name("filesystem", "read_file") == "filesystem_read_file"
