<!-- Parent: ../../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# bot_project

## Purpose
Primary end-to-end reference example — a QQ Bot demonstrating all ModexAgent framework subsystems. Shows how to wire Hook, Interceptor, Control, multi-agent, memory, plugin, and skill systems together in a real application.

## Key Files
| File | Description |
|------|-------------|
| `bot_service.py` | Entry point — `BotService` with pipeline/pool mode selection, full runtime assembly |
| `README.md` | Setup and run instructions |
| `.env.example` | Environment variable template |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `bot/` | Bot implementation — service, adapters, plugins, tools, logging, config (see `bot/AGENTS.md`) |
| `config/` | YAML configuration files |
| `plugins/` | Project-local plugins (`mem0_memory`, `tool_call_cleanup`) |
| `skills/` | SKILL.md-based skill directories — `main/`, `peers/`, `subagents/` |
| `tests/` | Bot-specific integration tests |

## For AI Agents

### Working In This Directory
- Do NOT modify `bot_service.py` imports — it has path setup and logging bootstrap that must run first
- Two runtime modes: `mode="pipeline"` (single `AgentPipeline`) and `mode="pool"` (`AgentPool` + `BrokerBridgeService`)
- Configuration in `config/bot_config.yml` — LLM, memory, tools, multi_agent, plugins all in one file
- Bot uses QQ platform adapters (`QQInputAdapter`, `QQOutputAdapter`, `QQBotEmitter`)

### Runtime Architecture (Pipeline mode)
```
QQ Gateway → QQInputAdapter → AgentPipeline
  → Routing → Dedup → Busy Check → Context Load
  → Agent.run() → QQOutputAdapter → QQ Gateway
```

### Testing Requirements
- Run: `PYTHONPATH=. python -m pytest examples/bot_project/tests/ -v`
- Tests cover agent communication, inbox flush, session routing, peer auto-send

## Dependencies

### Internal
- `framework` — all core components
- `framework.extensions.llm` — `LiteLLMProvider`

### External
- `qq-botpy` — QQ Bot SDK
- `litellm` — LLM provider
- `fastapi` — HTTP gateway
## Current Runtime Status

The bot project is the primary full-mode reference. Its default interceptor chain
currently wires `ControlDrainInterceptor` and `ToolResultLimitInterceptor` only,
and runtime persistence should use `JsonFileRuntimeStateStore`. See
`docs/current-runtime.md`.
