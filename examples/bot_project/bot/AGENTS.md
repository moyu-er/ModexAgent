<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-02 -->

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
| `adapters/` | Input/output adapters for QQ, Telegram, WebSocket (see `adapters/AGENTS.md`) |
| `config/` | Typed config code — config domains, pool/MCP/skills/prompt stores (see `config/AGENTS.md`) |
| `eval/` | Eval harness package — task schema, agent harness, experiment runner, cassette replay, metrics, dataset curators, CLI (opt-in, separate process; no Langfuse SDK in the bot process; see `eval/AGENTS.md`) |
| `input_pipeline/` | Converged user-input stage pipeline (see `input_pipeline/AGENTS.md`) |
| `service/` | Service lifecycle and pool orchestration (see `service/AGENTS.md`) |
| `webui/` | WebUI backend — server, events, transcript store (see `webui/AGENTS.md`) |
| `tools/` | Custom bot-specific tools |
| `utils/` | Configuration loading, media processing utilities |
| `web/` | Built static assets for the React frontend (auto-generated, do not edit) |

## For AI Agents

### Working In This Directory
- `service/core.py` is the main orchestration hub — it owns a `workspace_stack` (multi-live `ScopeRegistry` + controller + dispatcher, assembled by `bot/workspace/wiring/`) that lazily materializes per-workspace resources and wires them into pools, broker, and input pipeline. Workspace switching mutates only a per-session pointer (`SessionWorkspaceMap`) — there is no activation/deactivation. Pool assembly itself is declaration-driven (scope YAML → `bot/service/pool/declaration.py`).
- `input_pipeline/` is the converged message processing layer — all user messages pass through it before reaching `PoolRouter`.
- Native pool assembly exposes the `SkillsSupply`-bound root `SkillResolver` on `PoolInstance`; `SkillParseStage` performs per-pool lookup only. External pools expose no resolver.
- Changes to initialization flow should preserve the `build_workspace_stack` → `registry.materialize(home_context)` → pool creation order.
- `web/dist/` is rebuilt by `cd webui && npm run build` — never edit files there directly.

### Common Patterns
- Adapters follow the `InputAdapter` ABC (`modex_agent/pipeline/adapters.py`) and `OutputAdapter` ABC (`modex_agent/adapters/output.py`).
- Pool creation goes through `create_pool()` in `pool/`, not `AgentPool` directly.
- Workspace switching mutates only a per-session pointer (`SessionWorkspaceMap`); resources are lazy + cached + evictable via `ScopeRegistry` — no `on_activate`/`on_deactivate` callbacks.
- Per-pool data (memory, runtime stores, experience) lives on the workspace's `R.pool_data[pool]`; `PoolInstance` holds only deployment-level resources.
- In-flight turns hold a `PipelineSnapshot` with pinned workspace references, unaffected by mid-turn switches.
- Per-pool data (memory, runtime stores, experience) lives on the workspace's `R.pool_data[pool]`; `PoolInstance` holds only deployment-level resources.

## Dependencies

### Internal
- `modex_agent/` — core agent, pipeline, multi_agent, memory, workspace, tools modules

### External
- `aiohttp` — HTTP/WS server for WebUI
- `qq-botpy` — QQ Bot SDK
- `python-telegram-bot` — Telegram Bot API (long-polling)
