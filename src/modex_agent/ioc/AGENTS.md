<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# ioc

## Purpose
Inversion of Control container — typed configuration (Pydantic) and factory functions. Root `AppConfig` drives all subsystem assembly through 11 config objects and 7 factory modules.

## Key Files
| File | Description |
|------|-------------|
| `merge.py` | Config merge utilities |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `configs/` | Typed configuration dataclasses (11 files) |
| `factories/` | Assembly factory functions (7 files) |

### configs/
`app.py` (root `AppConfig`), `llm.py` (`LLMConfig`), `memory.py` (`MemoryConfig`), `safety.py` (`SafetyConfig`), `mcp.py` (`MCPConfig`), `approval.py` (`ApprovalConfig`), `hooks.py` (`HooksConfig`), `observability.py` (`ObservabilityConfig`), `plugins.py` (`PluginsConfig`), `skills.py` (`SkillsConfig`) — all loaded from YAML via `AppConfig`

### factories/
`app.py` (app factory), `llm.py` (LLM factory), `memory.py` (memory factory), `tools.py` (tools factory), `governance.py` (governance factory), `descriptors.py` (descriptor factory), `__init__.py`

## For AI Agents
- `AppConfig` is the root config loaded from YAML; all other configs are nested fields
- Each factory reads its config section and returns fully wired subsystem instances
- Config classes are pure data (frozen dataclasses); factories own all construction logic

## Dependencies
- `pydantic` — config model validation
- Consumed by `modex_agent/runtime/` (AgentRuntimeServices), `modex_agent/pipeline/` (AgentPipeline assembly)

<!-- MANUAL: -->
