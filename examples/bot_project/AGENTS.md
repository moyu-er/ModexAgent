<!-- Parent: ../../AGENTS.md -->
<!-- Generated: 2026-05-16 | Updated: 2026-05-22 -->

# bot_project

Primary end-to-end reference — a QQ Bot demonstrating all ModexAgent framework subsystems.

## Key Files

| File | Description |
|------|-------------|
| `bot_service.py` | Entry point — `QQBotService` with pipeline/pool mode selection, IOC `AppConfig.from_yaml()` wiring |
| `bot/service/core.py` | `BotService` — orchestration lifecycle, initialization sequence, interceptor/hook/control wiring |
| `bot/service/builders.py` | `AgentBuilderMixin` — standard tool registration, MCP tools, multi-agent tools, peer memory/skill construction |
| `bot/adapters/qq.py` | QQ platform adapters (`QQInputAdapter`, `QQOutputAdapter`, `QQBotEmitter`) |
| `bot/plugins/integration.py` | `PluginIntegration` facade |
| `bot/tools/custom.py` | `SendFileToUserTool` |
| `bot/utils/config_loader.py` | YAML/JSON config loading |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `bot/` | Bot implementation — service, adapters, plugins, tools, logging |
| `config/` | YAML configuration files (`bot_config.yml`), `mcp.json` |
| `plugins/` | Project-local plugins (`mem0_memory`, `tool_call_cleanup`) |
| `skills/` | SKILL.md-based skill directories — `main/`, `subagents/` |
| `data/` | Runtime data — scripts, PPT generation helpers |

## For AI Agents

### Working In This Directory
- Two runtime modes: `mode="pipeline"` (single `AgentPipeline`) and `mode="pool"` (`AgentPool` + `BrokerBridgeService`)
- Configuration in `config/bot_config.yml` — IOC `AppConfig.from_yaml()` as single source; `.env` for secrets
- Each peer/subagent gets isolated Memory/ToolManager/SkillManager/ContextManager
- Default interceptor chain: `ControlDrainInterceptor` + `ToolResultLimitInterceptor`
- Governance chain: `lossy_compaction` → `tool_chain_repair` → `token_budget`

### Multi-Agent Setup (Pool mode)
```
QQ Gateway → QQInputAdapter → MessageBroker → AgentPool
  → Main agent consumes broker messages
  → Main agent LLM calls send_message_async/dispatch_task tools
  → Target peer inbox receives message → wakeup → AgentPipeline.process_message()
  → Peer result sent back to main agent inbox → SubagentAutoSendHook safety net
```

### Peer Agent Wiring (`_initialize_peer_agents`)
1. `build_peer_descriptor()` creates `AgentDescriptor` with per-agent LLM, system prompt, memory, tools
2. Per-agent MCP tool injection via `mcp_filter` config
3. `SendMessageAsyncTool` registered on each peer (allowed_targets=[parent_name])
4. `SubagentAutoSendHook` attached to peer pipeline for safety net
5. `NullOutputAdapter` prevents peer LLM output from leaking to user
6. `SubagentAgentValidator.validate()` enforces star topology

### Testing
- Run: `PYTHONPATH=. python -m pytest examples/bot_project/tests/ -v`
- Tests cover: agent communication, inbox flush, session routing, peer auto-send, memory construction, compression config, runtime defaults

## Dependencies

### Internal
- `framework` — all core components

### External
- `qq-botpy` — QQ Bot SDK
- `litellm` — LLM provider
