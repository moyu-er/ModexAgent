from framework.ioc.configs.mcp import MCPConfig, MCPServerEntry


class TestMCPConfig:
    def test_defaults(self) -> None:
        cfg = MCPConfig()
        assert cfg.servers == {}
        assert cfg.tool_prefix == "mcp"

    def test_stdio_server(self) -> None:
        cfg = MCPConfig(
            servers={
                "playwright": MCPServerEntry(
                    command="npx",
                    args=["@playwright/mcp"],
                )
            }
        )
        assert cfg.servers["playwright"].command == "npx"
        assert cfg.servers["playwright"].args == ["@playwright/mcp"]

    def test_sse_server(self) -> None:
        cfg = MCPConfig(
            servers={
                "fetch": MCPServerEntry(
                    transport="sse",
                    url="https://mcp.example.com/sse",
                    headers={"Authorization": "Bearer token"},
                )
            }
        )
        assert cfg.servers["fetch"].transport == "sse"
        assert cfg.servers["fetch"].headers["Authorization"] == "Bearer token"

    def test_sse_server_via_type_alias(self) -> None:
        """`type` is accepted as input alias for `transport` (mcp.json convention)."""
        cfg = MCPConfig.model_validate(
            {"servers": {"fetch": {"type": "sse", "url": "https://x/sse"}}}
        )
        assert cfg.servers["fetch"].transport == "sse"
        # Serialization uses `transport`, not `type`.
        dumped = cfg.servers["fetch"].model_dump(exclude_none=True)
        assert dumped["transport"] == "sse"
        assert "type" not in dumped
