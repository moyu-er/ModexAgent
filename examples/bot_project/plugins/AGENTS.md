<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# plugins

Bot plugins — extensible modules that hook into the agent lifecycle. Currently plugins are **disabled by default** in pool mode.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `tool_call_cleanup/` | Tool call overflow cleanup plugin |

## tool_call_cleanup

Cleans up oversized tool call results after turn completion to prevent context bloat.

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `manager.py` | Plugin manager — registers hooks for post-turn cleanup |
| `policy.py` | Cleanup policy — determines which tool results to prune |

## For AI Agents

### Working In This Directory
- Plugins follow the `PluginIntegration` interface from `bot/plugins/integration.py`.
- Plugins are disabled in pool mode (`config={"enabled": False}`) — see `core.py` initialization.
- To enable, set `plugins.enabled: true` in `bot_config.yml` or modify the initialization code.
