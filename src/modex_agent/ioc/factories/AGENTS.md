<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-09-02 -->

# factories

## Purpose

Pure factory functions that consume Pydantic config objects from `modex_agent/ioc/configs/` and produce fully-wired runtime subsystem instances. Each factory handles its own section of the configuration and returns a ready-to-use object. No factory imports from `bot_project/`.

## Key Files

| File | Description |
|------|-------------|
| `llm.py` | `create_llm_provider(config, safety)` — routes all three `InterfaceFormat` values (OPENAI_COMPATIBLE / OPENAI_RESPONSE / ANTHROPIC) to `HTTPStreamProvider` wired with the matching protocol engine (openai_compat / openai_responses / anthropic, ADR-0046); resolves the endpoint URL (`endpoint_url` verbatim, else the engine's `url()` join on the normalized `base_url`); wraps safety config into `RuntimeSafetyPolicy` |
| `memory.py` | `create_memory(cfg, llm_provider, workspace)` — creates `DefaultMemorySystem` from `MemoryConfig`, converting Pydantic config to `MemoryLayerConfigSet` |
| `tools.py` | `connect_mcp(mcp_config, *, registry=None)` — connects to MCP servers (optional `registry` = ADR-0017 shared-connection overlay; when set, wraps a `SharedMcpBackend` from `registry.acquire` instead of a private `MCPClientManager`); `register_mcp_tools(adapter, tool_manager)` — registers MCP tools; `create_tool_manager(tools)` — creates pre-populated `InMemoryToolManager` |
| `governance.py` | `create_governance(cfg, ...)` — builds `CompositeGovernance` chain (lossy compaction → tool chain repair → final legality); `create_subagent_governance(cfg, ...)` — lightweight governance (no compaction) |
| `descriptors.py` | `build_session_only_memory(...)` + `DEFAULT_SYSTEM_PROMPT`, consumed by `AgentTemplate`; subagent descriptor/tool/resolver assembly lives on the unified materialization path. |
| `__init__.py` | Re-exports all factory functions from sub-modules |

## For AI Agents

### Working In This Directory
- Factories are stateless pure functions — no caching, no singletons, no shared mutable state
- Factory signatures follow the pattern: `create_*(config, *optional_overrides) -> Object | None`
- All factories return `None` when config is `None` (disabled feature)
- `create_llm_provider` routes on `interface_format` (ADR-0046): OPENAI_COMPATIBLE / OPENAI_RESPONSE / ANTHROPIC all construct `HTTPStreamProvider` wired with the matching protocol engine. Model names are NOT processed at all — `config.model` passes verbatim (user ruling 2026-08-26: a stale `openai/`/`anthropic/` prefix simply reaches the API as part of the model name; the API's "model not found" is the correct error). The factory resolves the endpoint URL (endpoint_url override verbatim, else the engine `url()` join) and passes one resolved `url` into the provider
- `descriptors.py` owns only session-memory construction and the default subagent prompt
- The `tools.py` factories are async (`connect_mcp`) because MCP server initialization requires network calls

### Common Patterns
- Safety config is folded into `RuntimeSafetyPolicy` at factory time, not stored as raw config
- MCP server config is converted to a dict before passing to `MCPClientManager`

## Dependencies

### Internal
- `modex_agent/ioc/configs/` — all Pydantic config models consumed here
- `modex_agent/providers/http/` — `HTTPStreamProvider` + protocol engines consumed by the LLM factory (ADR-0046); the only provider implementation — the legacy SDK providers were removed (2026-08-26 cleanup)
- `modex_agent/memory/` — `DefaultMemorySystem`, `MemoryLayerConfigSet`, governance classes
- `modex_agent/tools/` — `MCPClientManager`, `MCPToolAdapter`, `InMemoryToolManager`, standard tools
- `modex_agent/multi_agent/` — `AgentDescriptor`, `AgentAddress`, `AgentCommKind`

<!-- MANUAL -->
