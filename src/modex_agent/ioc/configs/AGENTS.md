<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# configs

## Purpose

Pydantic configuration models for every framework component. Each file defines a frozen data class consumed by factory functions to wire up runtime subsystems. `AppConfig` is the root aggregate — it is loaded from a single YAML file and all sub-configs are nested fields.

## Key Files

| File | Description |
|------|-------------|
| `app.py` | `AppConfig` — root configuration aggregating all sub-configs; includes `PathsConfig`, `SessionRetentionConfig`, `MultiAgentConfig`, `WorkspaceConfig`; resolves `${VAR}` env references from YAML |
| `agent.py` | `AgentConfig` — per-agent config (name, role, LLM, system prompt, tools, memory, skills, approval, safety, hooks, experience sub-config) |
| `llm.py` | `LLMConfig` — LLM provider settings (model, api_key, base_url, temperature, max_tokens) |
| `memory.py` | `MemoryConfig` — memory subsystem config (short-term, user retention, long-term, archive, knowledge layers with GovernanceConfig) |
| `safety.py` | `SafetyConfig` — LLM safety timeouts (`LLMSafetyConfig`) and turn safety timeouts (`TurnSafetyConfig`) |
| `approval.py` | `ApprovalConfig` — tool approval enable/disable with per-tool `ToolApprovalEntry` (allowed_paths) |
| `hooks.py` | `HooksConfig` — hook enable/disable list; default enables `logging` and `runtime_context` |
| `mcp.py` | `MCPConfig` — MCP server connections (stdio/sse/streamableHttp transport, command, args, env) |
| `observability.py` | `ObservabilityConfig` — logging and tracing enable/disable |
| `plugins.py` | `PluginConfig` — plugin system enable/disable with per-plugin configurations |
| `pool.py` | `PoolConfig` — per-pool config (LLM, agents, MCP, memory, skills, terminal settings); pool identity = the agent with role='main' |
| `skills.py` | `SkillsConfig` — skill auto-discovery roots and optional whitelist |
| `__init__.py` | Module docstring only: "Pydantic configuration models for each framework component" |

## For AI Agents

### Working In This Directory
- All config classes use `pydantic.BaseModel` and are frozen data carriers — no logic beyond validation
- `AppConfig` is the single entry point for full-app YAML loading; individual configs can be used directly for component-level testing
- `AppConfig` performs environment variable resolution (`${VAR:-default}` syntax) recursively through all string values
- `AgentConfig.tools` is populated in code, never from YAML
- `MemoryConfig` is the most complex — contains governance chain config nested inside it
- Config objects are consumed exclusively by `modex_agent/ioc/factories/`

### Common Patterns
- `None` means "disabled" / "not configured" (e.g., `MemoryConfig | None` in AgentConfig)
- Non-None defaults mean the feature is enabled by default (e.g., `HooksConfig` with default `[logging, runtime_context]`)
- Factory functions check `if cfg is None: return None` and fall back to defaults

## Dependencies

### Internal
- `pydantic` — all config models inherit from `BaseModel`
- `modex_agent/ioc/factories/` — each factory reads its corresponding config section
- `modex_agent/core/tool_manager.py` — `Tool` class used in `AgentConfig.tools`

<!-- MANUAL -->
