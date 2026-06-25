"""Agent Framework Plugin System.

Convention-based plugin extension mechanism for the agent framework.

Plugins are discovered from three sources (in priority order):
1. Bundled plugins: `plugins/bundled/`
2. User plugins: `~/.af/plugins/`
3. PyPI entry_points: `framework.plugins` group

Each plugin is a directory with `__init__.py` containing:
    def register(ctx: PluginContext) -> None:
        ctx.register_tool(MyTool())
        ctx.register_memory_provider(MyProvider())

Usage:
    pm = PluginManager()
    pm.discover_and_load(plugin_configs)

    loader = PluginLoader(pm)
    loader.inject_tools(tool_manager)
    loader.inject_hooks(hooks)
    await loader.inject_memory_providers(memory_system, init_kwargs={...})
"""

from modex_agent.plugins.abc import MemoryProvider
from modex_agent.plugins.context import PluginContext
from modex_agent.plugins.loader import PluginLoader
from modex_agent.plugins.manager import PluginManager

__all__ = [
    "MemoryProvider",
    "PluginContext",
    "PluginLoader",
    "PluginManager",
]
