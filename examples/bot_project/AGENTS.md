<!-- Parent: ../../AGENTS.md -->
<!-- Generated: 2026-05-16 | Updated: 2026-05-16 -->

# bot_project

Primary end-to-end reference — a QQ Bot demonstrating all ModexAgent framework subsystems.

## Key Files

| File | Description |
|------|-------------|
| `bot_service.py` | Entry point — `BotService` with pipeline/pool mode selection, full runtime assembly |
| `bot/service/core.py` | `BotService` — orchestration lifecycle, initialization sequence, stop sequence |
| `bot/service/builders.py` | `AgentBuilderMixin` — tool registration, memory creation, peer construction |
| `bot/adapters/qq.py` | QQ platform adapters (`QQInputAdapter`, `QQOutputAdapter`, `QQBotEmitter`) |
| `bot/plugins/integration.py` | `PluginIntegration` facade |
| `bot/tools/custom.py` | `SpawnSubagentTool`, `SendFileToUserTool` |
| `bot/utils/config_loader.py` | YAML/JSON config with `${VAR}` interpolation |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `bot/` | Bot implementation — service, adapters, plugins, tools, logging |
| `config/` | YAML configuration files (`bot_config.yml`) |
| `plugins/` | Project-local plugins (`mem0_memory`, `tool_call_cleanup`) |
| `skills/` | SKILL.md-based skill directories — `main/`, `peers/`, `subagents/` |
| `tests/` | Bot-specific tests |

## For AI Agents

### Working In This Directory
- Two runtime modes: `mode="pipeline"` (single `AgentPipeline`) and `mode="pool"` (`AgentPool` + `BrokerBridgeService`)
- Configuration in `config/bot_config.yml` — IOC `AppConfig.from_yaml()` as single source
- Each peer/subagent gets isolated Memory/ToolManager/SkillManager/ContextManager
- Default interceptor chain: `ControlDrainInterceptor` + `ToolResultLimitInterceptor`
- Governance chain: `lossy_compaction` → `tool_chain_repair` → `token_budget`

### Runtime Architecture (Pool mode)
```
QQ Gateway → QQInputAdapter → BrokerBridgeService → AgentPool
  → Agent.run() → QQOutputAdapter → QQ Gateway
```

### Testing
- Run: `PYTHONPATH=. python -m pytest examples/bot_project/tests/ -v`
- Tests cover: agent communication, inbox flush, session routing, peer auto-send, memory construction, compression config, runtime defaults

## Dependencies

### Internal
- `framework` — all core components

### External
- `qq-botpy` — QQ Bot SDK
- `litellm` — LLM provider
