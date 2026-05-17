<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 -->

# plugins

## Purpose
Plugin system — convention-based extensibility. Three discovery sources: bundled > user (`~/.af/plugins/`) > PyPI. Extension points: MemoryProvider, Tools, Hooks, SkillSources, MemorySystem modifiers.

## Key Files
| File | Description |
|------|-------------|
| `abc.py` | `MemoryProvider` ABC — `add()`, `search()`, `prefetch()`, `on_pre_compress()`, `system_prompt_block()` |
| `context.py` | `PluginContext` — registration facade: `register_tool()`, `register_memory_provider()`, `register_hook()`, `register_skill_source()`, `add_memory_system_modifier()` |
| `loader.py` | `PluginLoader` — injection bridge between plugin modules and framework |
| `manager.py` | `PluginManager` — three-source discovery and lifecycle (bundled > user > PyPI) |

## For AI Agents
- Plugin contract: `def register(ctx: PluginContext) -> None:`
- `PluginContext` is the sole registration surface; plugins never import framework internals directly
- `MemoryProvider` ABC provides the memory extension point with search and prefetch semantics
- ReAct clean mode runs without plugin-provided runtime services unless full mode re-enables them
