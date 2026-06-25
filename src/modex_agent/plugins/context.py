"""Plugin registration context.

Each plugin receives its own PluginContext instance during register(ctx).
Components are collected here and later injected by PluginLoader.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from modex_agent.core.skills.source import SkillSource
from modex_agent.core.tool_manager import Tool
from modex_agent.hook import Hook
from modex_agent.plugins.abc import MemoryProvider

if TYPE_CHECKING:
    from modex_agent.memory.system import MemorySystemContextManager
    from modex_agent.runtime.models import JsonValue


class PluginContext:
    """Plugin registration context.

    Design:
    - Only collects components; no actual registration (done by PluginLoader).
    - Each plugin gets an independent instance.
    - get_config() reads from bot_config.yml plugins.configurations section.
    """

    def __init__(self, plugin_name: str, config: dict[str, Any] | None = None) -> None:
        self._name = plugin_name
        self._config = config or {}

        # Collected components (not yet injected)
        self._tools: list[Tool] = []
        self._hooks: list[Hook] = []
        self._memory_providers: list[MemoryProvider] = []
        self._skill_sources: list[SkillSource] = []
        self._memory_system_modifiers: list[tuple[Callable[[Any], None], str]] = []

    @property
    def name(self) -> str:
        return self._name

    def get_config(self, key: str, default: JsonValue = None) -> JsonValue:
        """Read plugin-specific config from bot_config.yml.

        plugins:
          configurations:
            my_plugin:
              key: value
        """
        return self._config.get(key, default)

    # ---- registration methods ----

    def register_tool(self, tool: Tool) -> None:
        """Register a tool to be injected into ToolManager."""
        self._tools.append(tool)

    def register_hook(self, hook: Hook) -> None:
        """Register a Hook to be injected into pipeline hooks."""
        self._hooks.append(hook)

    def register_memory_provider(self, provider: MemoryProvider) -> None:
        """Register a MemoryProvider to be injected into MemorySystem."""
        self._memory_providers.append(provider)

    def register_skill_source(self, source: SkillSource) -> None:
        """Register a SkillSource to be injected into SkillManager."""
        self._skill_sources.append(source)

    def register_memory_system_modifier(
        self, modifier: Callable[[MemorySystemContextManager], None]
    ) -> None:
        """Register a callback that mutates a MemorySystem after initialization.

        Use case: plugins that need to wrap/override a MemorySystem's
        internal managers (e.g. wrapping the session manager with a
        policy-aware decorator via MemoryLayerSet.with_session()).

        The modifier receives the MemorySystem instance and can modify
        its internal state (e.g. wrapping the session manager via
        memory_system.layers.with_session()).
        """
        self._memory_system_modifiers.append((modifier, self._name))

    # ---- internal ----

    def collect(self) -> dict[str, list]:
        """Collect all registered components (used by PluginManager)."""
        return {
            "tools": list(self._tools),
            "hooks": list(self._hooks),
            "memory_providers": list(self._memory_providers),
            "skill_sources": list(self._skill_sources),
            "memory_system_modifiers": list(self._memory_system_modifiers),
        }
