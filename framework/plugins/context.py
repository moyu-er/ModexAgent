"""Plugin registration context.

Each plugin receives its own PluginContext instance during register(ctx).
Components are collected here and later injected by PluginLoader.
"""

from collections.abc import Callable
from typing import Any

from framework.core.hooks import AgentRunHook
from framework.core.skills.source import SkillSource
from framework.core.tool_manager import Tool
from framework.plugins.abc import MemoryProvider


class PluginContext:
    """Plugin registration context.

    Design:
    - Only collects components; no actual registration (done by PluginLoader).
    - Each plugin gets an independent instance.
    - get_config() reads from bot_config.yml plugins.configurations section.
    """

    def __init__(self, plugin_name: str, config: dict[str, Any] | None = None):
        self._name = plugin_name
        self._config = config or {}

        # Collected components (not yet injected)
        self._tools: list[Tool] = []
        self._hooks: list[AgentRunHook] = []
        self._memory_providers: list[MemoryProvider] = []
        self._skill_sources: list[SkillSource] = []
        self._memory_system_modifiers: list[tuple[Callable[[Any], None], str]] = []

    @property
    def name(self) -> str:
        return self._name

    def get_config(self, key: str, default: Any = None) -> Any:
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

    def register_hook(self, hook: AgentRunHook) -> None:
        """Register an AgentRunHook to be injected into pipeline hooks."""
        self._hooks.append(hook)

    def register_memory_provider(self, provider: MemoryProvider) -> None:
        """Register a MemoryProvider to be injected into MemorySystem."""
        self._memory_providers.append(provider)

    def register_skill_source(self, source: SkillSource) -> None:
        """Register a SkillSource to be injected into SkillManager."""
        self._skill_sources.append(source)

    def register_memory_system_modifier(
        self, modifier: Callable[[Any], None]
    ) -> None:
        """Register a callback that mutates a MemorySystem after initialization.

        Use case: plugins that need to wrap/override a MemorySystem's
        internal managers (e.g. replacing the short_term manager with a
        policy-aware decorator).

        The modifier receives the MemorySystem instance and can modify
        its internal state (e.g. memory_system._managers.short_term).
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
