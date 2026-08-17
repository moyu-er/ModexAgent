<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# mcp

## Purpose
Model Context Protocol (MCP) integration layer. Provides client implementations for stdio, SSE, and streamable HTTP transports, a client manager for unified connection lifecycle, and tool wrappers that adapt MCP tools to the framework `Tool` interface.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `client.py` | `BaseMCPClient` ABC + `StdioMCPClient`, `SSEMCPClient`, `StreamableHttpMCPClient`. Supports `TransportType.STDIO`, `TransportType.SSE`, `TransportType.STREAMABLE_HTTP`. Handles connection lifecycle, tool listing, and tool execution |
| `connection.py` | `connect_single_server` — reusable per-server connect primitive (transport detect + stdio/sse/streamable_http client creation). Owns `MCPConnectionError` |
| `registry.py` | `McpConnectionRegistry` + `SharedMcpBackend` — opt-in shared-connection overlay (ADR-0017). Service-scoped singleton deduplicating connections by canonical config-hash, connecting all servers concurrently via per-server supervisor tasks (anyio-safe same-task stack lifecycle), and exposing a `McpBackend` facade over the READY subset (gating by absence). Supervisors idle forever and service reconnect requests with exponential backoff (in-place stack/client swap, anyio-safe); the facade detects dropped connections passively and retries once via `request_reconnect` (coalesced) — parity with `MCPClientManager`, no active health polling |
| `manager.py` | `MCPClientManager` — unified management of MCP connections. Auto-registers tools from MCP servers. Supports automatic reconnection (including per-call reconnect-on-disconnect for `execute_tool`), config reload, and connection health monitoring |
| `tool.py` | MCP tool wrappers — `_extract_nullable_branch()` for nullable union handling, functions to convert MCP tool definitions to framework `Tool` objects |

## For AI Agents

### Working In This Directory
- Three transport types: `stdio` (local subprocess), `sse` (server-sent events), `streamable_http` (HTTP streaming)
- `MCPClientManager` auto-registers all tools from configured MCP servers
- Tool timeout default: 30 seconds (configurable)
- Stdio transport has pollution detection: "parse error", "invalid json", "unexpected token" markers in stderr trigger warnings
- Automatic reconnection on connection loss via `MCPClientManager`

### Common Patterns
- Configure MCP servers in the app config, then `MCPClientManager` creates connections and registers all tools automatically
- MCP tools are wrapped as `MCPToolAdapter` (implements `Tool` ABC) for seamless framework integration
- Tool parameters with nullable union types (e.g., `string | null`) are automatically unwrapped to the non-null branch
- `MCPConnectionError` raised on connection failures

## Dependencies

### Internal
- `modex_agent.core.tool_manager` — `Tool` ABC, `ToolConfig`

### External
- `mcp` — official MCP SDK (`ClientSession`, `McpError`)
- `httpx` — HTTP client for SSE and streamable HTTP transports

<!-- MANUAL -->
