<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# bot

Core business logic for the ModexAgent bot — service lifecycle, I/O adapters, tools, WebUI backend, and utilities.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `logging.py` | Logging configuration for the bot process |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `adapters/` | Input/output adapters for QQ, WebSocket (see `adapters/AGENTS.md`) |
| `service/` | Service lifecycle and pool orchestration (see `service/AGENTS.md`) |
| `webui/` | WebUI backend — server, events, transcript store (see `webui/AGENTS.md`) |
| `tools/` | Custom bot-specific tools |
| `utils/` | Configuration loading, media processing utilities |
| `plugins/` | Plugin integration |
| `web/` | Built static assets for the React frontend (auto-generated, do not edit) |

## For AI Agents

### Working In This Directory
- `service/core.py` is the main orchestration hub — it wires together workspace, pool, broker, adapters, and callbacks.
- Changes to initialization flow should preserve the workspace callback registration order (stop_and_rebuild before terminal_reset).
- `web/dist/` is rebuilt by `cd webui && npm run build` — never edit files there directly.

### Common Patterns
- Adapters follow `InputAdapter`/`OutputAdapter` ABC from `framework/pipeline/adapters.py`.
- Pool creation goes through `create_pool()` in `pool_builder.py`, not `AgentPool` directly.
- Workspace switching uses callback pattern — new subsystems register via `workspace_context.register_callback()`.

## Dependencies

### Internal
- `framework/` — core agent, pipeline, multi_agent, memory, workspace, tools modules

### External
- `aiohttp` — HTTP/WS server for WebUI
- `nonebot2` / `aiocqhttp` — QQ platform integration
