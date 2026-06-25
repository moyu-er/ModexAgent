<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# tools

## Purpose

Tool subsystem — registry, type definitions, filtering, metadata parsing, MCP integration, the stateful terminal system, standard built-in tools, AST code-analysis tools, LSP integration, web tools, tool-result overflow management, and workspace-scoped tools. All tools implement the `Tool` ABC from `modex_agent/core`.

## Key Files

| File | Description |
|------|-------------|
| `registry.py` | `ToolRegistry` — tool registration, lookup, and catalog management |
| `types.py` | Tool-related type definitions (767 lines) — `ToolParameter`, `ToolSpec`, `ToolResult`, `ToolCall` |
| `filter.py` | `FilteredToolManager` — per-agent tool visibility and access control |
| `metadata_parser.py` | Rich docstring parser (Google/NumPy/Sphinx styles) — extracts parameter schemas for automatic tool definition |
| `presets.py` | Tool preset definitions — named sets of tools for different agent configurations |
| `mcp_adapter.py` | `MCPToolAdapter`, `MCPToolRegistry` — bridges MCP protocol tools to the framework `Tool` interface |
| `workspace_scoped.py` | Workspace-scoped tool wrappers — resolve relative paths against bound workspace root instead of process CWD (wraps read/write/edit/ls/find/search/bash) |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `mcp/` | MCP integration — `BaseMCPClient`, `MCPClientManager`, connection management, tool wrapper; transports: stdio, SSE, streamable HTTP |
| `standard/` | Built-in tools — `file_tool.py` (read/write/edit/ls/find), `search_tool.py` (content search across files) |
| `terminal/` | Stateful terminal system — `TerminalManager`, `TerminalSession`, `ShellTool`, input guard, poll loop, process-registry, config, state-store, types, prompt; backends: pexpect, tmux, Windows visible/hidden, winpty (see `terminal/AGENTS.md`) |
| `overflow/` | Tool result overflow management — `ToolOverflowStore` ABC, `ToolResultOverflowHandler`, `OverflowCleaner`, `OverflowStore` local implementation (see `overflow/AGENTS.md`) |
| `ast/` | AST-based code analysis — `ast_search.py`, `ast_replace.py`, `engine.py` |
| `web/` | Web tools — `reader.py` (URL content extraction), `search.py` (web search) |
| `lsp/` | Language Server Protocol integration — `lsp_navigation.py`, `lsp_diagnostics.py` |

## For AI Agents

- New tools: subclass `Tool` ABC with `name`, `description`, `parameters`, and `execute()` method
- `MCPClientManager` auto-registers tools/resources/prompts from MCP servers with automatic reconnection
- `FilteredToolManager` enforces per-agent tool visibility rules at runtime
- `metadata_parser.py` enables automatic parameter schema extraction from docstrings
- Terminal sessions are stateful — use session IDs for persistent shell interaction
- No standalone `executor.py` exists; tool execution is handled by `ToolRegistry` lookup + individual tool `.execute()` calls, coordinated by the agent's `ToolNode` (see `modex_agent/agents/AGENTS.md`)

## Dependencies

- `modex_agent.core.abc` — `Tool` ABC for all tool implementations
- `modex_agent.agents.react.nodes.tool` — `ToolNode` that orchestrates tool batch execution and approval
- `modex_agent.workspace` — `WorkspaceContext` for workspace-scoped tool resolution
