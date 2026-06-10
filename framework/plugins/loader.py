"""Plugin component injection into framework modules.

Decouples PluginManager from framework internals.
Each inject_* method is independent and can be called separately.
"""

import logging

from framework.core.skills.manager import SkillManager
from framework.hook import Hook
from framework.memory.system import MemorySystemContextManager
from framework.plugins.manager import PluginManager
from framework.core.tool_manager import InMemoryToolManager

logger = logging.getLogger(__name__)


class PluginLoader:
    """Inject collected plugin components into framework modules.

    Usage:
        loader = PluginLoader(plugin_manager)
        loader.inject_tools(tool_manager)
        loader.inject_hooks(pipeline_hooks)
        await loader.inject_memory_providers(memory_system, init_kwargs={...})
        loader.inject_skill_sources(skill_manager)
    """

    def __init__(self, plugin_manager: PluginManager):
        self._pm = plugin_manager

    def inject_tools(self, tool_manager: InMemoryToolManager) -> list[str]:
        """Inject plugin tools into ToolManager.

        Args:
            tool_manager: InMemoryToolManager instance with register(tool) method.

        Returns:
            List of successfully injected tool names.
        """
        injected = []
        for tool, plugin_name in self._pm.tools:
            try:
                tool_manager.register(tool)
                injected.append(tool.name)
            except Exception as e:
                logger.warning(
                    "Failed to inject tool '%s' from plugin '%s': %s",
                    tool.name, plugin_name, e,
                )
        if injected:
            logger.info("Injected %d plugin tools: %s", len(injected), injected)
        return injected

    def inject_hooks(self, hooks: list[Hook]) -> list[str]:
        """Append plugin hooks to a hooks list.

        Args:
            hooks: BotService's pipeline_hooks list.

        Returns:
            List of injected hook class names.
        """
        injected = []
        for hook, plugin_name in self._pm.hooks:
            hooks.append(hook)
            injected.append(f"{type(hook).__name__} (from {plugin_name})")
        if injected:
            logger.info("Injected %d plugin hooks: %s", len(injected), injected)
        return injected

    async def inject_memory_providers(
        self,
        memory_system: MemorySystemContextManager,
        init_kwargs: dict[str, object] | None = None,
    ) -> list[str]:
        """Inject plugin MemoryProviders into MemorySystem and initialize them.

        Initialization is delegated to PluginManager.initialize_providers()
        (which is idempotent), then all available providers are added to
        the MemorySystem.

        Args:
            memory_system: MemorySystem instance with add_provider() method.
            init_kwargs: kwargs passed to provider.initialize().

        Returns:
            List of successfully injected provider names.
        """
        await self._pm.initialize_providers(**(init_kwargs or {}))

        injected: list[str] = []
        for provider in self._pm.available_providers:
            try:
                memory_system.add_provider(provider)
                injected.append(provider.name)
                logger.info(
                    "Provider '%s' injected into MemorySystem", provider.name
                )
            except Exception as e:
                logger.warning(
                    "Failed to add provider '%s' to MemorySystem: %s",
                    provider.name, e,
                )
        return injected

    def inject_memory_system_modifiers(
        self, memory_system: MemorySystemContextManager
    ) -> list[str]:
        """Apply plugin MemorySystem modifiers.

        Modifiers are callbacks that receive a MemorySystem instance and can
        mutate its internal state (e.g. wrapping managers). Called after
        MemorySystem.initialize() and before the system is used.

        Args:
            memory_system: MemorySystem instance.

        Returns:
            List of applied modifier plugin names.
        """
        applied: list[str] = []
        for modifier, plugin_name in self._pm.memory_system_modifiers:
            try:
                modifier(memory_system)
                applied.append(plugin_name)
                logger.info(
                    "Applied memory_system modifier from '%s'",
                    plugin_name,
                )
            except Exception as e:
                logger.warning(
                    "Failed to apply memory_system modifier from '%s': %s",
                    plugin_name, e,
                )
        return applied

    def inject_skill_sources(self, skill_manager: SkillManager) -> list[str]:
        """Inject plugin SkillSources into SkillManager.

        Args:
            skill_manager: SkillManager instance with add_source() method.

        Returns:
            List of successfully injected source identifiers.
        """
        injected = []
        for source, plugin_name in self._pm.skill_sources:
            try:
                skill_manager.add_source(source)
                injected.append(plugin_name)
            except Exception as e:
                logger.warning(
                    "Failed to inject skill source from '%s': %s",
                    plugin_name, e,
                )
        if injected:
            logger.info("Injected %d plugin skill sources", len(injected))
        return injected
