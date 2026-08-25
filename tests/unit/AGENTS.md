<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# tests/unit

Unit tests for all `src/modex_agent/` modules, mirroring the package structure one-to-one.

## Purpose

Each subdirectory here corresponds to a `src/modex_agent/` module and contains unit tests for its public APIs. Tests focus on isolated component behavior with mocked dependencies.

## Subdirectories

| Directory | Target Module |
|-----------|--------------|
| `agents/` | `modex_agent/agents/` — ReAct agent, experience review, summarizer |
| `approval/` | `modex_agent/approval/` — approval tiers, response parsing |
| `commands/` | `modex_agent/commands/` — slash command parser, processor |
| `config_roster/` | `modex_agent/config/` — roster loader, SpecBuilder |
| `core/` | `modex_agent/core/` — agent ABC, session store, tool manager, emitter |
| `hook/` | `modex_agent/hook/` — hook runner, builtin hooks |
| `interceptor/` | `modex_agent/interceptor/` — interceptor chain, builtin interceptors |
| `ioc/` | `modex_agent/ioc/` — config models, factory modules |
| `memory/` | `modex_agent/memory/` — three-layer memory, consolidation, injection |
| `messaging/` | `modex_agent/messaging/` — broker, broker bridge |
| `multi_agent/` | `modex_agent/multi_agent/` — pool, bus, communication tracker, inbox |
| `pipeline/` | `modex_agent/pipeline/` — pipeline orchestration, adapters, snapshot |
| `plugins/` | `modex_agent/plugins/` — component registry, loader, assembly pipeline, defaults |
| `providers/` | `modex_agent/providers/` — LLM provider implementations |
| `runtime/` | `modex_agent/runtime/` — runtime services, store, codec |
| `sandbox/` | `modex_agent/sandbox/` — isolation, adapters, guards |
| `tools/` | `modex_agent/tools/` — registry, executor, MCP, terminal, standard tools |
| `trace/` | `modex_agent/trace/` — trace store, hooks |
| `utils/` | `modex_agent/utils/` — file_io, sanitizer, deduplicator, timezone |
| `workspace/` | `modex_agent/workspace/` — workspace context, registry, routing |
| `bot/` | `examples/bot_project/bot/` — lifecycle, pool routing, adapters |
| `bot_project/` | `examples/bot_project/` — config loader, slash commands, MCP resilience |

## For AI Agents

### Working In This Directory
- Run a single module's tests: `pytest tests/unit/<module>/ -v`
- Test names follow `test_<function/class>_<scenario>` convention
- `conftest.py` files in each subdirectory provide shared fixtures

<!-- MANUAL -->
