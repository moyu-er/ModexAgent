"""BotService 与插件系统的集成封装。

将 bot_service.py 中散落的插件相关逻辑集中到此处，
避免 1800+ 行的服务文件继续膨胀。
"""

import logging
from pathlib import Path
from typing import Any

from modex_agent.plugins import PluginLoader, PluginManager

logger = logging.getLogger(__name__)


class PluginIntegration:
    """封装 BotService 与插件系统的集成逻辑。

    Usage:
        integration = PluginIntegration(config)
        await integration.initialize(tool_manager)
        await integration.inject_memory_providers(memory_system, init_kwargs=...)
        integration.inject_skill_sources(skill_manager)
        hooks = integration.collect_hooks()
        ...
        await integration.shutdown()
    """

    def __init__(
        self,
        config: dict[str, Any],
        extra_plugin_dirs: list[Path] | None = None,
    ) -> None:
        self.config = config
        self.plugin_manager = PluginManager()
        self._extra_plugin_dirs = list(extra_plugin_dirs) if extra_plugin_dirs else []

    # ---- discovery ----

    async def discover_and_load(self) -> bool:
        """Load plugins and inject tools into ToolManager.

        Plugins are disabled by default. To enable, add `plugins.enabled: true`
        in bot_config.yml. Each plugin still respects its own
        `configurations[<name>].enabled` setting.

        Returns:
            True if any plugins were loaded.
        """
        plugins_section = self.config.get("plugins") or {}
        # Top-level kill switch — missing or False blocks all plugin loading.
        top_enabled = plugins_section.get("enabled", False)
        if not top_enabled:
            return False

        plugin_configs = dict(plugins_section.get("configurations") or {})
        # `enabled` at the top level can also be a list = whitelist
        if isinstance(top_enabled, list):
            plugin_configs["_enabled"] = list(top_enabled)

        # Scan extra local plugin directories before discover_and_load
        for extra_dir in self._extra_plugin_dirs:
            if extra_dir.exists():
                self.plugin_manager.scan_directory(extra_dir, plugin_configs)
                logger.debug("Scanned local plugins directory: %s", extra_dir)

        self.plugin_manager.discover_and_load(plugin_configs)

        return bool(self.plugin_manager.list_plugins())

    def get_loader(self) -> PluginLoader:
        return PluginLoader(self.plugin_manager)

    # ---- injection helpers ----

    def inject_tools(self, tool_manager: Any) -> list[str]:
        """注入插件工具到 ToolManager。"""
        if not self.plugin_manager.tools:
            return []
        loader = self.get_loader()
        return loader.inject_tools(tool_manager)

    async def inject_memory_providers(
        self,
        *memory_systems: Any,
        init_kwargs: dict[str, Any] | None = None,
    ) -> list[str]:
        """注入插件 MemoryProvider 到一个或多个 MemorySystem。"""
        if not self.plugin_manager.memory_providers:
            return []
        loader = self.get_loader()
        initialized: list[str] = []
        for ms in memory_systems:
            result = await loader.inject_memory_providers(
                ms,
                init_kwargs=init_kwargs,
            )
            initialized.extend(result)
        return initialized

    def inject_skill_sources(self, skill_manager: Any) -> list[str]:
        """注入插件 SkillSource 到 SkillManager。"""
        if not self.plugin_manager.skill_sources:
            return []
        loader = self.get_loader()
        return loader.inject_skill_sources(skill_manager)

    def collect_hooks(self) -> list[Any]:
        """收集插件 Hooks（供 AgentFactory / Pipeline 使用）。"""
        if not self.plugin_manager.hooks:
            return []
        hooks: list[Any] = []
        loader = self.get_loader()
        loader.inject_hooks(hooks)
        return hooks

    def inject_memory_system_modifiers(self, memory_system: Any) -> list[str]:
        """应用插件 MemorySystem 修饰器。

        在 MemorySystem.initialize() 之后调用，让插件可以修改
        MemorySystem 的内部状态（如包装 session manager）。
        """
        if not self.plugin_manager.memory_system_modifiers:
            return []
        loader = self.get_loader()
        return loader.inject_memory_system_modifiers(memory_system)

    # ---- lifecycle ----

    async def shutdown(self) -> None:
        """关闭所有插件 Provider。"""
        await self.plugin_manager.shutdown_providers()

    # ---- query ----

    def list_plugins(self) -> list[dict[str, str]]:
        return self.plugin_manager.list_plugins()
