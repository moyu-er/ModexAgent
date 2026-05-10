<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# plugins

## Purpose
Plugin system — convention-based extensibility. Three discovery sources: bundled > user (`~/.af/plugins/`) > PyPI. Extension points: MemoryProvider, Tools, Hooks, SkillSources, MemorySystem modifiers.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Plugin system exports |
| `context.py` | `PluginContext` — registration facade for tools, memory providers, hooks |

## For AI Agents

### Working In This Directory
- Plugin contract: `def register(ctx: PluginContext) -> None:`
- `ctx.register_tool(...)`, `ctx.register_memory_provider(...)`, `ctx.register_hook(...)`
- `MemoryProvider` ABC: `add()`, `search()`, `prefetch()` methods
## Current Runtime Status

Plugins may contribute hooks, tools, or policies, but ReAct clean mode should run
without plugin-provided runtime services unless explicitly re-enabled by full
mode assembly.
