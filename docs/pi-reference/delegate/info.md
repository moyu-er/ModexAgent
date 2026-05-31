# Agent: delegate

| Property                    | Value                                                                     |
| --------------------------- | ------------------------------------------------------------------------- |
| **Source**                  | builtin                                                                   |
| **Description**             | Lightweight subagent that inherits the parent model with no default reads |
| **System Prompt Mode**      | append                                                                    |
| **Inherit Project Context** | true                                                                      |
| **Inherit Skills**          | false                                                                     |

## Required Tools

| Tool                 | Description                                                                                                                                                                         |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `read`               | Read file contents. Supports text files and images (jpg, png, gif, webp). Images sent as attachments. Text output truncated to 2000 lines / 50KB. Use offset/limit for large files. |
| `grep`               | AST-aware code pattern search. Use specific AST patterns (e.g., `function $NAME() { $$$BODY }`), NOT text search. Prefer specific patterns with context.                            |
| `find`               | AST-aware code pattern replace. Dry-run by default (use apply=true to apply). Use specific AST patterns, not text.                                                                  |
| `ls`                 | List files in a directory. Returns file names and types (file/directory).                                                                                                           |
| `bash`               | Execute a bash command in the current working directory. Returns stdout and stderr. Output truncated to last 2000 lines / 50KB. Optional timeout in seconds.                        |
| `edit`               | Make precise file edits with exact text replacement. Multiple disjoint edits in one call. Each oldText matched against original file (not incrementally). No overlapping edits.     |
| `write`              | Create or overwrite files. Automatically creates parent directories if needed. Use for new files or complete rewrites.                                                              |
| `contact_supervisor` | Contact the supervisor (parent) agent that spawned this subagent. Cannot contact other subagents. Requires reason: "need_decision" or "progress_update".                            |

## Communication Constraints

| Capability                                  | Detail                                                                           |
| ------------------------------------------- | -------------------------------------------------------------------------------- |
| **Can dispatch subagents?**                 | No — does not have `subagent` tool. Cannot spawn further agents.                 |
| **Can communicate with parent?**            | Yes — via `contact_supervisor`. Reaches the agent that dispatched this subagent. |
| **Can communicate with sibling subagents?** | No. Star topology: all communication routes through the main agent.              |
| **Who receives its output?**                | Returns directly to the parent agent that spawned it.                            |
