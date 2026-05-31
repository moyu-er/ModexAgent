<!-- Parent: ../../AGENTS.md -->
<!-- Updated: 2026-05-31 | Branch: develop_gyt | Commit: 6647e8a -->

# bot_project

Primary end-to-end reference for the ModexAgent framework. Demonstrates Pool mode (multi-agent collaboration) and Pipeline mode (single-agent service). Full README with configuration guide at `examples/bot_project/README.md`.

## Key Files

| File | Description |
| --- | --- |
| `bot_service.py` | Entry point with pipeline/pool mode selection and IOC config loading |
| `bot/service/core.py` | `BotService` orchestration lifecycle and runtime wiring |
| `bot/service/builders.py` | Tool registration, MCP tools, subagent memory/skill construction, terminal setup |
| `bot/adapters/qq.py` | QQ platform input/output adapters (C2C + group + file upload) |
| `config/bot_config.yml` | Agent, memory, tool, runtime, observability configuration. Supports `${ENV_VAR}` interpolation |
| `config/mcp.json` | MCP server configuration (stdio/SSE/streamable_http transports) |

## Multi-Agent Setup

- `main` is a normal agent with all MCP tools, file/shell tools, and communication tools.
- `office-expert` is a subagent with file/shell tools and docx/pdf/pptx/xlsx skills.
- `query-12306` is a subagent with MCP tools (12306-mcp, fetch).
- Communication tools: `send_to_agent` (sync), `send_to_agent_async` (inbox-based), `list_communication_targets`.
- The old `send_message`, `send_message_async`, and `dispatch_task` tools are **not used**.
- `SubagentAutoSendHook` auto-forwards subagent output to parent if LLM forgets communication tools.
- Session ID format: `{conversation_id}:{agent_name}[:{invocation_id}]` (via `DefaultSessionIdStrategy`).

## Testing

```powershell
python -m pytest examples/bot_project/tests -q
```
