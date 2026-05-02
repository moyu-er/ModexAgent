<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# tools

## Purpose
Tool subsystem — registry, executor, MCP integration, standard tools. All tools implement the `Tool` ABC and are discovered/registered via `ToolRegistry`.

## Key Files
| File | Description |
|------|-------------|
| `registry.py` | `ToolRegistry` — tool registration and discovery |
| `executor.py` | Tool execution engine |
| `types.py` | Tool-related type definitions |
| `toolkit.py` | Toolkit management |
| `mcp_adapter.py` | MCP protocol adapter |
| `metadata_parser.py` | Tool metadata parsing |
| `secure_wrapper.py` | Security wrapping for tool execution |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `mcp/` | MCP integration — `MCPClientManager`, `MCPToolAdapter`, three transports (stdio, SSE, streamable_http) |
| `standard/` | Standard built-in tools — `shell_tool.py`, `file_tool.py` |

## For AI Agents

### Working In This Directory
- New tools: subclass `Tool` ABC with `name`, `description`, `parameters`, `execute()` method
- `MCPClientManager` auto-registers tools/resources/prompts from MCP servers
- Three MCP transports: `stdio`, `sse`, `streamable_http`
- Auto-reconnection for MCP server disconnects

### Common Patterns
```python
class MyTool(Tool):
    name = "my_tool"
    description = "Does something useful"
    parameters = {...}  # JSON Schema

    async def execute(self, **kwargs) -> ToolResult:
        ...
```
## Current Runtime Status

Tools execute through the ReAct `ToolNode`. Approval, cancellation metadata, and
runtime control boundaries should be handled by the runtime services described in
`docs/current-runtime.md`.
