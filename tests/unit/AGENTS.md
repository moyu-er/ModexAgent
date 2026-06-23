<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# tests/unit

Unit tests for all `framework/` modules, mirroring the package structure one-to-one.

## Purpose

Each subdirectory here corresponds to a `framework/` module and contains unit tests for its public APIs. Tests focus on isolated component behavior with mocked dependencies.

## Subdirectories

| Directory | Target Module |
|-----------|--------------|
| `agents/` | `framework/agents/` — ReAct agent, experience review, summarizer |
| `approval/` | `framework/approval/` — approval tiers, response parsing |
| `commands/` | `framework/commands/` — slash command parser, processor |
| `core/` | `framework/core/` — agent ABC, session store, tool manager, emitter |
| `hook/` | `framework/hook/` — hook runner, builtin hooks |
| `interceptor/` | `framework/interceptor/` — interceptor chain, builtin interceptors |
| `ioc/` | `framework/ioc/` — config models, factory modules |
| `memory/` | `framework/memory/` — three-layer memory, consolidation, injection |
| `messaging/` | `framework/messaging/` — broker, broker bridge |
| `multi_agent/` | `framework/multi_agent/` — pool, bus, communication tracker, inbox |
| `pipeline/` | `framework/pipeline/` — pipeline orchestration, adapters, snapshot |
| `plugins/` | `framework/plugins/` — plugin manager, loader |
| `providers/` | `framework/providers/` — LLM provider implementations |
| `runtime/` | `framework/runtime/` — runtime services, store, codec |
| `sandbox/` | `framework/sandbox/` — isolation, adapters, guards |
| `tools/` | `framework/tools/` — registry, executor, MCP, terminal, standard tools |
| `trace/` | `framework/trace/` — trace store, hooks |
| `utils/` | `framework/utils/` — file_io, sanitizer, deduplicator, timezone |
| `workspace/` | `framework/workspace/` — workspace context, registry, routing |
| `bot/` | `examples/bot_project/bot/` — lifecycle, pool routing, adapters |
| `bot_project/` | `examples/bot_project/` — config loader, slash commands, MCP resilience |

## For AI Agents

### Working In This Directory
- Run a single module's tests: `pytest tests/unit/<module>/ -v`
- Test names follow `test_<function/class>_<scenario>` convention
- `conftest.py` files in each subdirectory provide shared fixtures

<!-- MANUAL -->
