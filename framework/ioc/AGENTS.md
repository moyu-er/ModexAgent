<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 -->

# ioc

## Purpose
Inversion of Control container — configuration dataclasses and factory functions. Root `AppConfig` drives all subsystem assembly through typed config objects and dedicated factory modules.

## Key Files
| File | Description |
|------|-------------|
| `merge.py` | Config merge utilities |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `configs/` | Typed configuration dataclasses |
| `factories/` | Assembly factory functions |

### configs/
`app.py` (root `AppConfig` from YAML), `agent.py` (`AgentConfig`), `llm.py` (`LLMConfig`), `memory.py` (`MemoryConfig`), `safety.py` (`SafetyConfig`), `mcp.py` (`MCPConfig`), `approval.py` (`ApprovalConfig`), `hooks.py` (`HooksConfig`), `observability.py` (`ObservabilityConfig`), `plugins.py` (`PluginsConfig`), `skills.py` (`SkillsConfig`)

### factories/
`app_factory`, `agent_factory`, `llm_factory`, `memory_factory`, `tools_factory`, `governance_factory`, `compression_factory`, `descriptors` — each assembles its subsystem from the corresponding config

## For AI Agents
- `AppConfig` is the root config loaded from YAML; all other configs are nested fields
- Each factory reads its config section and returns fully wired subsystem instances
- Config classes are pure data (frozen dataclasses); factories own all construction logic
