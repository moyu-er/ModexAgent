<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-27 -->

# plugins

Bot-side plugins for the component-factory-based plugin system. Each plugin
registers component factories into the ``ComponentRegistry`` (loaded by
``ComponentRegistryLoader`` in ``core.py`` initialization). The plugin
classes stay here; the underlying hook/strategy/stage CLASSES stay in their
respective ``bot/service/`` or ``bot/input_pipeline/`` directories (rule 9,
FW/BIZ separation).

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `bot_strategies.py` | `BotStrategiesPlugin` — registers `react` + `external` execution strategies into the `EXECUTION_STRATEGY` slot via `SimpleFactory` |
| `bot_hooks.py` | `BotHooksPlugin` — registers bot-specific React + Memory hooks (`model_choice_bind`, memory cleanup hooks) into the `HOOK` slot via `ReactHookFactory` / `MemoryHookFactory`, plus the `send_file_to_user` TOOL-slot factory (`SendFileToUserToolFactory` — output adapter/transcript/media/sessions-dir deps from the pool assembly context; declared per agent via `tools: [+send_file_to_user]`) |
| `im_input_stages.py` | `IMInputStagesPlugin` — registers every built-in IM/WebUI pipeline stage factory into the `INPUT_STAGE` slot; constructor dependencies use frozen config models |

## For AI Agents

### Working In This Directory
- Plugins follow the `Plugin` ABC from `modex_agent.plugins.loader` — implement `register(ctx: PluginRegistrationContext)`.
- Factories wrap existing bot-layer classes; the framework holds only the factory ABCs.
- Loaded by `ComponentRegistryLoader.load()` in `core.py` `initialize()` alongside `DefaultPlugin` (bundled FW defaults).
- To add a new plugin: create a module here with a `Plugin` subclass, then add it to the `bundled_factories` list in `core.py` or the `project_plugin_paths` discovery.
