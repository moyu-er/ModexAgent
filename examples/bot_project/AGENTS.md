<!-- Parent: ../../AGENTS.md -->
<!-- Updated: 2026-05-22 -->

# bot_project

Primary end-to-end reference for the ModexAgent framework.

## Key Files

| File | Description |
| --- | --- |
| `bot_service.py` | Entry point with pipeline/pool mode selection and IOC config loading |
| `bot/service/core.py` | `BotService` orchestration lifecycle and runtime wiring |
| `bot/service/builders.py` | Tool registration, MCP tools, subagent memory/skill construction |
| `bot/adapters/qq.py` | QQ platform input/output adapters |
| `config/bot_config.yml` | Agent, memory, tool, and runtime configuration |

## Multi-Agent Setup

- `main` is a normal agent.
- `office-expert` and `query-12306` are subagents.
- `bot_project` registers `send_to_agent_async` for async agent communication.
- `list_communication_targets` may be registered for target discovery.
- The old `send_message`, `send_message_async`, and `dispatch_task` tools are not used.

Subagent memory is scoped by the full receiver-owned session id:

```text
{conversation_id}:{agent_name}:{invocation_id}
```

## Testing

Run:

```powershell
python -m pytest examples/bot_project/tests -q
```
