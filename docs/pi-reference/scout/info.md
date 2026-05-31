# Agent: scout

| Property                    | Value                                                           |
| --------------------------- | --------------------------------------------------------------- |
| **Source**                  | builtin                                                         |
| **Description**             | Fast codebase recon that returns compressed context for handoff |
| **System Prompt Mode**      | replace                                                         |
| **Inherit Project Context** | true                                                            |
| **Inherit Skills**          | false                                                           |
| **Thinking**                | low                                                             |
| **Output**                  | context.md                                                      |
| **Progress Tracking**       | true                                                            |

## Required Tools

| Tool       | Description                                                                                                                                                                         |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `read`     | Read file contents. Supports text files and images (jpg, png, gif, webp). Images sent as attachments. Text output truncated to 2000 lines / 50KB. Use offset/limit for large files. |
| `grep`     | AST-aware code pattern search. Use specific AST patterns (e.g., `function $NAME() { $$$BODY }`), NOT text search. Prefer specific patterns with context.                            |
| `find`     | AST-aware code pattern replace. Dry-run by default (use apply=true to apply). Use specific AST patterns, not text.                                                                  |
| `ls`       | List files in a directory. Returns file names and types (file/directory).                                                                                                           |
| `bash`     | Execute a bash command in the current working directory. Returns stdout and stderr. Output truncated to last 2000 lines / 50KB. Optional timeout in seconds.                        |
| `write`    | Create or overwrite files. Automatically creates parent directories if needed. Use for new files or complete rewrites.                                                              |
| `intercom` | Inter-agent communication. Sends messages back to the parent/supervisor agent only. Cannot reach sibling subagents.                                                                 |

## Communication Constraints

| Capability                                  | Detail                                                                                       |
| ------------------------------------------- | -------------------------------------------------------------------------------------------- |
| **Can dispatch subagents?**                 | No — does not have `subagent` tool. Cannot spawn further agents.                             |
| **Can communicate with parent?**            | Yes — via `intercom`. Sends recon findings to the parent agent.                              |
| **Can communicate with sibling subagents?** | No. Star topology: all communication routes through the main agent.                          |
| **Who receives its output?**                | The parent agent. `context.md` is consumed by downstream agents (e.g., `planner`, `worker`). |
