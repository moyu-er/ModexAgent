"""Plugin discovery, loading, and lifecycle management.

Scans three sources for plugins (bundled, user, entry_points) and
loads them via the register(ctx) convention.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.core.skills.source import SkillSource
    from modex_agent.core.tool_manager import Tool
    from modex_agent.hook import Hook
    from modex_agent.runtime.models import JsonValue

from modex_agent.plugins.abc import MemoryProvider
from modex_agent.plugins.context import PluginContext

logger = logging.getLogger(__name__)

ENTRY_POINTS_GROUP = "modex_agent.plugins"


class PluginManager:
    """Plugin manager.

    Responsibilities:
    1. Discover and load plugins from three sources.
    2. Call each plugin's register(ctx) to collect components.
    3. Manage MemoryProvider lifecycle (initialize/shutdown).
    4. Provide component query interface.

    Usage:
        pm = PluginManager()
        pm.discover_and_load(plugin_configs)
        # ... inject via PluginLoader ...
        await pm.initialize_providers(llm_provider=..., workspace=...)
        # ... run ...
        await pm.shutdown_providers()
    """

    def __init__(self, user_plugins_dir: Path | None = None) -> None:
        self._contexts: dict[str, PluginContext] = {}
        self._user_plugins_dir = user_plugins_dir or Path.home() / ".af" / "plugins"

        # Collected components (populated after discover_and_load)
        self._tools: list[tuple[Tool, str]] = []  # (Tool, plugin_name)
        self._hooks: list[tuple[Hook, str]] = []  # (Hook, plugin_name)
        self._memory_providers: list[tuple[MemoryProvider, str]] = []  # (Provider, name)
        self._skill_sources: list[tuple[SkillSource, str]] = []  # (Source, plugin_name)
        self._memory_system_modifiers: list[
            tuple[Callable[[object], None], str]
        ] = []  # (modifier, plugin_name)
        self._providers_initialized = False
        self._initialized_provider_names: set[str] = set()

    # ========================================
    # Discovery & Loading
    # ========================================

    def discover_and_load(
        self, plugin_configs: dict[str, dict[str, JsonValue]] | None = None
    ) -> None:
        """Discover and load all plugins.

        Args:
            plugin_configs: plugins.configurations section from bot_config.yml.
                           key=plugin_name, value=config dict.
                           _enabled key restricts loading to named plugins.
        """
        configs = plugin_configs or {}

        # Priority: bundled -> user -> entry_points
        self._load_from_directory(Path(__file__).parent / "bundled", configs, source="bundled")
        self._load_from_directory(self._user_plugins_dir, configs, source="user")
        self._load_from_entry_points(configs)

        self._collect_all()

        logger.info(
            "Plugins loaded: %d plugins, %d tools, %d hooks, "
            "%d providers, %d skill_sources, %d modifiers",
            len(self._contexts),
            len(self._tools),
            len(self._hooks),
            len(self._memory_providers),
            len(self._skill_sources),
            len(self._memory_system_modifiers),
        )

    def _load_from_directory(self, directory: Path, configs: dict[str, dict], source: str) -> None:
        """Scan a directory for plugin subdirectories."""
        if not directory.exists():
            return

        for plugin_dir in sorted(directory.iterdir()):
            if not plugin_dir.is_dir():
                continue
            if not (plugin_dir / "__init__.py").exists():
                continue

            plugin_name = plugin_dir.name
            plugin_config = configs.get(plugin_name)

            # Optional enabled-list filtering
            enabled_list = configs.get("_enabled")
            if enabled_list is not None and plugin_name not in enabled_list:
                logger.debug("Plugin '%s' not in enabled list, skipping", plugin_name)
                continue

            self._load_single_plugin(plugin_dir, plugin_name, plugin_config, source)

    def _load_single_plugin(
        self,
        plugin_dir: Path,
        plugin_name: str,
        config: dict | None,
        source: str,
    ) -> None:
        """Load a single plugin from its directory as a proper package.

        Supports multi-file plugins with relative imports by registering
        the plugin as a package in sys.modules and pre-loading sibling
        .py files as submodules.
        """
        init_file = plugin_dir / "__init__.py"

        try:
            module_name = f"af_plugin_{plugin_name}"

            # ---- load as a package so relative imports work ----
            spec = importlib.util.spec_from_file_location(
                module_name,
                init_file,
                submodule_search_locations=[str(plugin_dir)],
            )
            if spec is None or spec.loader is None:
                logger.warning("Plugin '%s': invalid module spec", plugin_name)
                return

            module = importlib.util.module_from_spec(spec)
            module.__package__ = module_name
            sys.modules[module_name] = module

            # Pre-register and execute sibling .py files as submodules so
            # that "from .manager import X" resolves correctly. Sorted to
            # respect simple name-based dependencies (e.g. policy before manager).
            for py_file in sorted(plugin_dir.glob("*.py")):
                if py_file.name == "__init__.py":
                    continue
                sub_name = f"{module_name}.{py_file.stem}"
                sub_spec = importlib.util.spec_from_file_location(sub_name, py_file)
                if sub_spec is not None and sub_spec.loader is not None:
                    sub_module = importlib.util.module_from_spec(sub_spec)
                    sub_module.__package__ = module_name
                    sys.modules[sub_name] = sub_module
                    sub_spec.loader.exec_module(sub_module)

            spec.loader.exec_module(module)

            register_fn = getattr(module, "register", None)
            if register_fn is None:
                logger.warning("Plugin '%s': no register() function", plugin_name)
                return

            # First-seen wins: bundled > user > entry_points
            if plugin_name in self._contexts:
                logger.debug(
                    "Plugin '%s' already loaded from higher-priority source, skipping",
                    plugin_name,
                )
                return

            ctx = PluginContext(plugin_name=plugin_name, config=config)
            register_fn(ctx)
            self._contexts[plugin_name] = ctx

            logger.debug("Loaded plugin: %s (%s)", plugin_name, source)

        except Exception as e:
            logger.warning("Failed to load plugin '%s': %s", plugin_name, e)

    def _load_from_entry_points(self, configs: dict[str, dict]) -> None:
        """Discover plugins from PyPI entry_points."""
        try:
            eps = importlib.metadata.entry_points()
            if hasattr(eps, "select"):
                group_eps = eps.select(group=ENTRY_POINTS_GROUP)
            else:
                group_eps = eps.get(ENTRY_POINTS_GROUP, [])  # type: ignore[attr-defined]

            for ep in group_eps:
                try:
                    register_fn = ep.load()
                    plugin_name = ep.name

                    # Optional enabled-list filtering (same as _load_from_directory)
                    enabled_list = configs.get("_enabled")
                    if enabled_list is not None and plugin_name not in enabled_list:
                        logger.debug(
                            "Entry point plugin '%s' not in enabled list, skipping",
                            plugin_name,
                        )
                        continue

                    # First-seen wins: bundled / user take priority over entry_points
                    if plugin_name in self._contexts:
                        logger.debug(
                            "Plugin '%s' already loaded from higher-priority source, skipping",
                            plugin_name,
                        )
                        continue

                    ctx = PluginContext(
                        plugin_name=plugin_name,
                        config=configs.get(plugin_name),
                    )
                    register_fn(ctx)
                    self._contexts[plugin_name] = ctx
                except Exception as e:
                    logger.warning("Entry point plugin '%s' failed: %s", ep.name, e)
        except Exception:
            pass  # silently skip if entry_points are unavailable

    def _collect_all(self) -> None:
        """Aggregate components from all PluginContext instances.

        Idempotent: clears existing lists before re-collecting so that
        multiple calls (e.g. from scan_directory + discover_and_load)
        do not produce duplicates.
        """
        self._tools.clear()
        self._hooks.clear()
        self._memory_providers.clear()
        self._skill_sources.clear()
        self._memory_system_modifiers.clear()

        for plugin_name, ctx in self._contexts.items():
            collected = ctx.collect()
            for tool in collected["tools"]:
                self._tools.append((tool, plugin_name))
            for hook in collected["hooks"]:
                self._hooks.append((hook, plugin_name))
            for provider in collected["memory_providers"]:
                self._memory_providers.append((provider, plugin_name))
            for source in collected["skill_sources"]:
                self._skill_sources.append((source, plugin_name))
            for modifier, mod_plugin_name in collected.get("memory_system_modifiers", []):
                self._memory_system_modifiers.append((modifier, mod_plugin_name))

    def scan_directory(self, directory: Path, configs: dict[str, dict] | None = None) -> None:
        """Scan an additional directory for plugins and collect their components.

        Intended for project-local plugins that live alongside the application
        code rather than in the global user or bundled directories.

        Args:
            directory: Path to a directory containing plugin subdirectories.
            configs: Same format as discover_and_load plugin_configs.
        """
        self._load_from_directory(directory, configs or {}, source="local")
        self._collect_all()

    # ========================================
    # Component Access (for PluginLoader)
    # ========================================

    @property
    def tools(self) -> list[tuple[Any, str]]:
        return list(self._tools)

    @property
    def hooks(self) -> list[tuple[Any, str]]:
        return list(self._hooks)

    @property
    def memory_providers(self) -> list[tuple[MemoryProvider, str]]:
        return list(self._memory_providers)

    @property
    def available_providers(self) -> list[MemoryProvider]:
        """Return providers that successfully initialized."""
        return [p for p, _ in self._memory_providers if p.name in self._initialized_provider_names]

    @property
    def skill_sources(self) -> list[tuple[Any, str]]:
        return list(self._skill_sources)

    @property
    def memory_system_modifiers(self) -> list[tuple[Any, str]]:
        return list(self._memory_system_modifiers)

    # ========================================
    # Provider Lifecycle
    # ========================================

    async def initialize_providers(self, **kwargs: JsonValue) -> list[str]:
        """Initialize all MemoryProviders.

        Per-provider error isolation: one failing provider does not block others.
        Idempotent: subsequent calls after the first are no-ops.

        Args:
            **kwargs: passed to provider.initialize() (llm_provider, workspace, ...)

        Returns:
            List of successfully initialized provider names.
        """
        if self._providers_initialized:
            logger.debug("Providers already initialized, skipping")
            return []
        self._providers_initialized = True

        initialized = []
        for provider, plugin_name in self._memory_providers:
            if not provider.is_available():
                logger.warning(
                    "Provider '%s' (plugin: %s) not available, skipping",
                    provider.name,
                    plugin_name,
                )
                continue
            try:
                await provider.initialize(**kwargs)
                initialized.append(provider.name)
                self._initialized_provider_names.add(provider.name)
                logger.info("Provider '%s' initialized", provider.name)
            except Exception as e:
                logger.error(
                    "Provider '%s' (plugin: %s) init failed: %s",
                    provider.name,
                    plugin_name,
                    e,
                )
        return initialized

    async def shutdown_providers(self) -> None:
        """Shutdown all MemoryProviders."""
        for provider, _ in self._memory_providers:
            try:
                await provider.shutdown()
            except Exception as e:
                logger.warning("Provider '%s' shutdown error: %s", provider.name, e)

    # ========================================
    # Info Query
    # ========================================

    def list_plugins(self) -> list[dict[str, str]]:
        """List all loaded plugins with component summary."""
        result = []
        for name, ctx in self._contexts.items():
            collected = ctx.collect()
            components = []
            if collected["tools"]:
                components.append(f"{len(collected['tools'])} tools")
            if collected["hooks"]:
                components.append(f"{len(collected['hooks'])} hooks")
            if collected["memory_providers"]:
                components.append(f"{len(collected['memory_providers'])} providers")
            if collected["skill_sources"]:
                components.append(f"{len(collected['skill_sources'])} skills")
            if collected["memory_system_modifiers"]:
                components.append(f"{len(collected['memory_system_modifiers'])} modifiers")
            result.append(
                {
                    "name": name,
                    "components": ", ".join(components) or "none",
                }
            )
        return result
