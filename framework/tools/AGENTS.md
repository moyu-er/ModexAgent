<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# tools

## Purpose
Tool subsystem — registry, executor, MCP integration, terminal system, overflow management, standard tools, filtering, and metadata. All tools implement the `Tool` ABC.

## Key Files
| File | Description |
|------|-------------|
| `registry.py` | `ToolRegistry` — tool registration and lookup |
| `types.py` | Tool-related type definitions (767 lines) |
| `toolkit.py` | Toolkit with AOP hooks (730 lines) |
| `filter.py` | `FilteredToolManager` — per-agent tool visibility |
| `mcp_adapter.py` | `MCPToolAdapter`, `MCPToolRegistry` — bridges MCP to framework `Tool` |
| `metadata_parser.py` | Rich docstring parser (Google/NumPy/Sphinx styles) |
| `secure_wrapper.py` | Security wrapping for tool execution |
| `presets.py` | Tool preset definitions |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `mcp/` | MCP integration — `BaseMCPClient`, `MCPClientManager`, connection management, tool wrapper; stdio/SSE/streamable_http transports |
| `standard/` | Built-in tools — `file_tool.py`, `search_tool.py`, `shell_tool.py` |
| `terminal/` | Stateful terminal — `TerminalManager`, `TerminalSession`, `ShellTool`, input guard, poll loop, pexpect/tmux/winpty backends (see `terminal/AGENTS.md`) |
| `overflow/` | Tool result overflow — `ToolOverflowStore` ABC, `ToolResultOverflowHandler`, `OverflowCleaner` (see `overflow/AGENTS.md`) |
| `ast/` | AST-based code analysis engine |
| `web/` | Web tools — reader, search |
| `lsp/` | Language Server Protocol integration |

## For AI Agents
- New tools: subclass `Tool` ABC with `name`, `description`, `parameters`, `execute()` method
- `MCPClientManager` auto-registers tools/resources/prompts from MCP servers with reconnection
- `FilteredToolManager` enforces per-agent tool visibility rules
- `metadata_parser.py` extracts parameter schemas from docstrings for automatic tool definition
