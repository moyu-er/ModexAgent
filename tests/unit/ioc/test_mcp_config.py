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
                    type="sse",
                    url="https://mcp.example.com/sse",
                    headers={"Authorization": "Bearer token"},
                )
            }
        )
        assert cfg.servers["fetch"].type == "sse"
        assert cfg.servers["fetch"].headers["Authorization"] == "Bearer token"
