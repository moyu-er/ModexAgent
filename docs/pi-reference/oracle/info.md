# Agent: oracle

| Property                    | Value                                                                                     |
| --------------------------- | ----------------------------------------------------------------------------------------- |
| **Source**                  | builtin                                                                                   |
| **Description**             | High-context decision-consistency oracle that protects inherited state and prevents drift |
| **System Prompt Mode**      | replace                                                                                   |
| **Inherit Project Context** | true                                                                                      |
| **Inherit Skills**          | false                                                                                     |
| **Default Context**         | fork                                                                                      |
| **Thinking**                | high                                                                                      |

## Required Tools

| Tool       | Description                                                                                                                                                                         |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `read`     | Read file contents. Supports text files and images (jpg, png, gif, webp). Images sent as attachments. Text output truncated to 2000 lines / 50KB. Use offset/limit for large files. |
| `grep`     | AST-aware code pattern search. Use specific AST patterns (e.g., `function $NAME() { $$$BODY }`), NOT text search. Prefer specific patterns with context.                            |
| `find`     | AST-aware code pattern replace. Dry-run by default (use apply=true to apply). Use specific AST patterns, not text.                                                                  |
| `ls`       | List files in a directory. Returns file names and types (file/directory).                                                                                                           |
| `bash`     | Execute a bash command in the current working directory. Returns stdout and stderr. Output truncated to last 2000 lines / 50KB. Optional timeout in seconds.                        |
| `intercom` | Inter-agent communication. Sends messages back to the parent/supervisor agent only. Cannot reach sibling subagents.                                                                 |

## Communication Constraints

| Capability                                  | Detail                                                                                   |
| ------------------------------------------- | ---------------------------------------------------------------------------------------- |
| **Can dispatch subagents?**                 | No — does not have `subagent` tool. Cannot spawn further agents.                         |
| **Can communicate with parent?**            | Yes — via `intercom`. Sends findings/consistency checks to the agent that dispatched it. |
| **Can communicate with sibling subagents?** | No. Star topology: all communication routes through the main agent.                      |
| **Who receives its output?**                | The parent agent. Used to validate decisions before execution continues.                 |
