<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-31 | Branch: develop_gyt | Commit: 6647e8a -->

# ioc

## Purpose
Inversion of Control container — typed configuration (Pydantic) and factory functions. Root `AppConfig` drives all subsystem assembly through 13 config objects and 8 factory modules.

## Key Files
| File | Description |
|------|-------------|
| `merge.py` | Config merge utilities |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `configs/` | Typed configuration dataclasses (13 files) |
| `factories/` | Assembly factory functions (8 files) |

### configs/
`app.py` (root `AppConfig`), `agent.py` (`AgentConfig`), `llm.py` (`LLMConfig`), `memory.py` (`MemoryConfig`), `safety.py` (`SafetyConfig`), `mcp.py` (`MCPConfig`), `approval.py` (`ApprovalConfig`), `hooks.py` (`HooksConfig`), `observability.py` (`ObservabilityConfig`), `plugins.py` (`PluginsConfig`), `skills.py` (`SkillsConfig`), `pool.py` (`PoolConfig`) — all loaded from YAML via `AppConfig`

### factories/
`app.py` (app factory), `agent.py` (agent factory), `llm.py` (LLM factory), `memory.py` (memory factory), `tools.py` (tools factory), `governance.py` (governance factory), `descriptors.py` (descriptor factory), `__init__.py`

## For AI Agents
- `AppConfig` is the root config loaded from YAML; all other configs are nested fields
- Each factory reads its config section and returns fully wired subsystem instances
- Config classes are pure data (frozen dataclasses); factories own all construction logic
