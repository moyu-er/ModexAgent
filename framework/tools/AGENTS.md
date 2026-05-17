<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 -->

# tools

## Purpose
Tool subsystem — registry, executor, MCP integration, standard tools, filtering, and metadata. All tools implement the `Tool` ABC and are discovered/registered via `ToolRegistry`.

## Key Files
| File | Description |
|------|-------------|
| `registry.py` | `ToolRegistry` — tool registration and lookup |
| `executor.py` | Tool execution engine |
| `types.py` | Tool-related type definitions |
| `toolkit.py` | Toolkit with AOP hooks |
| `filter.py` | `FilteredToolManager` — per-agent tool visibility |
| `mcp_adapter.py` | `MCPToolAdapter`, `MCPToolRegistry` — bridges MCP to framework `Tool` |
| `metadata_parser.py` | Rich docstring parser (Google/NumPy/Sphinx styles) |
| `secure_wrapper.py` | Security wrapping for tool execution |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `mcp/` | MCP integration — `MCPClientManager` (`client.py`), connection management (`manager.py`), tool wrapper (`tool.py`); three transports: stdio, SSE, streamable_http |
| `standard/` | Built-in tools — `file_tool.py`, `search_tool.py`, `shell_tool.py` |

## For AI Agents
- New tools: subclass `Tool` ABC with `name`, `description`, `parameters`, `execute()` method
- `MCPClientManager` auto-registers tools/resources/prompts from MCP servers with reconnection
- `FilteredToolManager` enforces per-agent tool visibility rules
- `metadata_parser.py` extracts parameter schemas from docstrings for automatic tool definition
