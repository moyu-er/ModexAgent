<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# factories

## Purpose

Pure factory functions that consume Pydantic config objects from `modex_agent/ioc/configs/` and produce fully-wired runtime subsystem instances. Each factory handles its own section of the configuration and returns a ready-to-use object. No factory imports from `bot_project/`.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `create_agent(cfg, default_llm_provider, default_safety)` — creates a `ReActAgent` from `AgentConfig` with LLM inheritance logic |
| `llm.py` | `create_llm_provider(config, safety)` — creates `LiteLLMProvider` or `OpenAIProvider` based on model name prefix; wraps safety config into `RuntimeSafetyPolicy` |
| `memory.py` | `create_memory(cfg, llm_provider, workspace)` — creates `DefaultMemorySystem` from `MemoryConfig`, converting Pydantic config to `MemoryLayerConfigSet` |
| `tools.py` | `connect_mcp(mcp_config)` — connects to MCP servers; `register_mcp_tools(adapter, tool_manager)` — registers MCP tools; `create_tool_manager(tools)` — creates pre-populated `InMemoryToolManager` |
| `governance.py` | `create_governance(cfg, ...)` — builds `CompositeGovernance` chain (lossy compaction → tool chain repair → final legality); `create_subagent_governance(cfg, ...)` — lightweight governance (no compaction) |
| `descriptors.py` | `build_subagent_descriptor(...)` — builds `AgentDescriptor` + tool_manager + skill_manager for subagents from `AppConfig`; contains standard tool builders (file, shell, search tools) |
| `__init__.py` | Re-exports all factory functions from sub-modules |

## For AI Agents

### Working In This Directory
- Factories are stateless pure functions — no caching, no singletons, no shared mutable state
- Factory signatures follow the pattern: `create_*(config, *optional_overrides) -> Object | None`
- All factories return `None` when config is `None` (disabled feature)
- `create_llm_provider` routes by model name prefix: `openai/` → native OpenAI SDK, otherwise → LiteLLM
- `descriptors.py` is the largest factory — it handles tool building, memory creation, and full descriptor assembly for subagents
- The `tools.py` factories are async (`connect_mcp`) because MCP server initialization requires network calls

### Common Patterns
- Safety config is folded into `RuntimeSafetyPolicy` at factory time, not stored as raw config
- MCP server config is converted to a dict before passing to `MCPClientManager`
- Agent factory handles LLM inheritance: if `AgentConfig.llm` is None, it uses the caller-provided default provider

## Dependencies

### Internal
- `modex_agent/ioc/configs/` — all Pydantic config models consumed here
- `modex_agent/agents/react/` — `ReActAgent` created by agent factory
- `modex_agent/providers/` — `LiteLLMProvider`, `OpenAIProvider` created by LLM factory
- `modex_agent/memory/` — `DefaultMemorySystem`, `MemoryLayerConfigSet`, governance classes
- `modex_agent/tools/` — `MCPClientManager`, `MCPToolAdapter`, `ToolRegistry`, standard tools
- `modex_agent/multi_agent/` — `AgentDescriptor`, `AgentAddress`, `AgentCommKind`

<!-- MANUAL -->
