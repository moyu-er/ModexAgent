# Agent: researcher

| Property                    | Value                                                                                     |
| --------------------------- | ----------------------------------------------------------------------------------------- |
| **Source**                  | builtin                                                                                   |
| **Description**             | Autonomous web researcher — searches, evaluates, and synthesizes a focused research brief |
| **System Prompt Mode**      | replace                                                                                   |
| **Inherit Project Context** | true                                                                                      |
| **Inherit Skills**          | false                                                                                     |
| **Thinking**                | medium                                                                                    |
| **Output**                  | research.md                                                                               |
| **Progress Tracking**       | true                                                                                      |

## Required Tools

| Tool                 | Description                                                                                                                                                                         |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `read`               | Read file contents. Supports text files and images (jpg, png, gif, webp). Images sent as attachments. Text output truncated to 2000 lines / 50KB. Use offset/limit for large files. |
| `write`              | Create or overwrite files. Automatically creates parent directories if needed. Use for new files or complete rewrites.                                                              |
| `web_search`         | Search the web. Used by researcher subagent for multi-angle research queries.                                                                                                       |
| `fetch_content`      | Fetch and return raw content from a URL. Used by researcher for deep source reading.                                                                                                |
| `get_search_content` | Get structured content from search results. Used by researcher for evidence gathering.                                                                                              |
| `intercom`           | Inter-agent communication. Sends messages back to the parent/supervisor agent only. Cannot reach sibling subagents.                                                                 |

## Communication Constraints

| Capability                                  | Detail                                                                               |
| ------------------------------------------- | ------------------------------------------------------------------------------------ |
| **Can dispatch subagents?**                 | No — does not have `subagent` tool. Cannot spawn further agents.                     |
| **Can communicate with parent?**            | Yes — via `intercom`. Sends research findings or asks for clarification from parent. |
| **Can communicate with sibling subagents?** | No. Star topology: all communication routes through the main agent.                  |
| **Who receives its output?**                | The parent agent. `research.md` is consumed by the main agent.                       |
