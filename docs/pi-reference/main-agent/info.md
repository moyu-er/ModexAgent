# Agent: main-agent

| Property               | Value                                                                                   |
| ---------------------- | --------------------------------------------------------------------------------------- |
| **Source**             | `@earendil-works/pi-coding-agent` — `dist/core/system-prompt.js`                        |
| **Role**               | User-facing orchestrator — receives user input, coordinates tools, dispatches subagents |
| **System Prompt Mode** | Dynamic build (skeleton + project context + skills + runtime info)                      |
| **Context**            | Full session history + project files (AGENTS.md, CLAUDE.md, etc.)                       |

## Required Tools

| Tool                   | Description                                                                                                                                                                                                                                                                               |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `read`                 | Read file contents. Supports text files and images (jpg, png, gif, webp). Images sent as attachments. Text output truncated to 2000 lines / 50KB. Use offset/limit for large files.                                                                                                       |
| `bash`                 | Execute a bash command in the current working directory. Returns stdout and stderr. Output truncated to last 2000 lines / 50KB. Optional timeout in seconds.                                                                                                                              |
| `edit`                 | Make precise file edits with exact text replacement. Multiple disjoint edits in one call. Each oldText matched against original file (not incrementally). No overlapping edits.                                                                                                           |
| `write`                | Create or overwrite files. Automatically creates parent directories if needed. Use for new files or complete rewrites.                                                                                                                                                                    |
| `web_reader_webReader` | Fetch and convert URL to Large Model Friendly Input (markdown by default). Optional image retention, GFM support, timeout.                                                                                                                                                                |
| `mcp`                  | MCP gateway — connect to MCP servers and call their tools. Non-MCP Pi tools should be called directly, not through mcp. Supports connect/describe/search/server/tool/action.                                                                                                              |
| `subagent`             | Delegate to subagents or manage agent definitions. Modes: SINGLE (one task), CHAIN (sequential pipeline), PARALLEL (concurrent execution). Supports async, forked context, intercom coordination. **Main-agent is the dispatcher — subagents cannot spawn further subagents by default.** |
| `ast_grep_search`      | Search code using AST-aware pattern matching. Use specific AST node patterns (function declarations, imports, method calls), NOT raw text.                                                                                                                                                |
| `ast_grep_replace`     | Replace code using AST-aware pattern matching. Dry-run by default (use apply=true). Use specific AST patterns.                                                                                                                                                                            |
| `lsp_diagnostics`      | Get errors, warnings, and hints from language servers for a file or directory. Works on directories by auto-detecting file extensions. Use BEFORE running builds.                                                                                                                         |
| `lsp_navigation`       | Navigate code using LSP (Language Server Protocol). Operations: definition, references, hover, signatureHelp, documentSymbol, workspaceSymbol, codeAction, rename, implementation, incomingCalls, outgoingCalls, workspaceDiagnostics.                                                    |

## Communication Constraints

| Capability                             | Detail                                                                                                      |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| **Can dispatch subagents?**            | ✅ Yes — `subagent` tool is the dispatcher. Can launch SINGLE, CHAIN, PARALLEL, async, forked-context runs. |
| **Can receive from subagents?**        | ✅ Yes — receives `intercom` messages and `contact_supervisor` calls from subagents it spawned.             |
| **Subagent → subagent communication?** | ❌ Not directly. All subagent communication routes through this main agent (star topology).                 |
| **Subagent can spawn more subagents?** | ❌ No. Only the main agent should create/manage the subagent tree.                                          |
