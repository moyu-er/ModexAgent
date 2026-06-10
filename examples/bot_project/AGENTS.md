<!-- Parent: ../../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# bot_project

Primary end-to-end reference for the ModexAgent framework. Demonstrates Pool mode (multi-agent collaboration) and Pipeline mode (single-agent service). Full README with configuration guide at `examples/bot_project/README.md`.

## Key Files

| File | Description |
| --- | --- |
| `bot_service.py` | Entry point with pipeline/pool mode selection and IOC config loading |
| `bot/service/core.py` | `BotService` orchestration lifecycle and runtime wiring |
| `bot/service/builders.py` | Tool registration, MCP tools, subagent memory/skill construction, terminal setup |
| `bot/service/pool_builder.py` | Pool mode assembly — creates `AgentPool`, subagent descriptors, experience manager |
| `bot/adapters/qq.py` | QQ platform input/output adapters (C2C + group + file upload) |
| `config/bot_config.yml` | Agent, memory, tool, runtime, observability configuration. Supports `${ENV_VAR}` interpolation |
| `config/mcp.json` | MCP server configuration (stdio/SSE/streamable_http transports) |

## Multi-Agent Setup

- `main` is a normal agent with all MCP tools, file/shell tools, and communication tools.
- `office-expert` is a subagent with file/shell tools and docx/pdf/pptx/xlsx skills.
- `query-12306` is a subagent with MCP tools (12306-mcp, fetch).
- Communication tools: `send_to_agent` (async inbox-based — description shows all available targets).
- The old `send_message`, `send_message_async`, and `dispatch_task` tools are **not used**.
- `SubagentAutoSendHook` auto-forwards subagent output to parent if LLM forgets communication tools.
- Session ID format: `{conversation_id}:{agent_name}[:{invocation_id}]` (via `DefaultSessionIdStrategy`).

## Skills Structure

```
skills/
├── coding/                    # Coding-related skills
│   ├── coding/                # Claude Code-style skills
│   │   ├── brainstorming/
│   │   ├── executing-plans/
│   │   ├── writing-plans/
│   │   ├── writing-skills/
│   │   ├── test-driven-development/
│   │   ├── systematic-debugging/
│   │   ├── verification-before-completion/
│   │   ├── self-improvement/
│   │   ├── subagent-driven-development/
│   │   ├── finishing-a-development-branch/
│   │   ├── requesting-code-review/
│   │   ├── using-git-worktrees/
│   │   ├── using-superpowers/
│   │   ├── find-skills/
│   │   └── deepinit/
│   └── reviewer/              # Code review skills
│       └── code-review-expert/
└── main/                      # Main agent skills
    └── main/
        ├── huashu-design/     # Video/slide design skill (TTS, rendering)
        ├── skill-creator/
        ├── find-skills/
        └── self-improve/
```

## Testing

```powershell
python -m pytest examples/bot_project/tests -q
```
