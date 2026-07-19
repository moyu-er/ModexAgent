<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-19 -->

# plugins

Bot plugins — extensible modules that hook into the agent lifecycle. Currently
plugins are **disabled by default** in pool mode and no concrete plugins ship
in this directory. The `tool_call_cleanup` plugin was removed (2026-07) — it
was unused and imported a module path that broke test collection.

## For AI Agents

### Working In This Directory
- Plugins follow the `PluginIntegration` interface from `bot/plugins/integration.py`.
- Plugins are disabled in pool mode (`config={"enabled": False}`) — see `core.py` initialization.
- To enable, set `plugins.enabled: true` in `bot_config.yml` and add a concrete plugin subdirectory here.
