<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# lsp

## Purpose
Language Server Protocol integration stubs for code navigation and diagnostics. Currently provides placeholder tool implementations that return a not-yet-implemented message. These will be wired to actual LSP clients in a future update.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `lsp_navigation.py` | `LspNavigationTool(Tool)` — stub for LSP code navigation operations: go_to_definition, find_references, hover, document_symbol, workspace_symbol, go_to_implementation, incoming_calls, outgoing_calls |
| `lsp_diagnostics.py` | `LspDiagnosticsTool(Tool)` — stub for retrieving LSP errors/warnings/hints for a file or directory |

## For AI Agents

### Working In This Directory
- Both tools are stubs — `execute()` returns a message stating the feature is not yet implemented
- The tool definitions (name, description, parameters) are already defined for future LLM compatibility
- When implemented, these tools will connect to language servers (via pyright/pylsp) for real-time code intelligence

### Common Patterns
- Tools follow the standard `Tool` ABC pattern with `name`, `description`, `parameters` properties
- Parameters are already defined for future use — the parameter schemas describe expected inputs

## Dependencies

### Internal
- `modex_agent.core.tool_manager` — `Tool` ABC

<!-- MANUAL -->
