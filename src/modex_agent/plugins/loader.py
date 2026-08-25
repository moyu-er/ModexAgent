"""Plugin(ABC) + PluginRegistrationContext + PluginDiscoveryConfig
+ ComponentRegistryLoader.

Replaces the legacy injection bridge with the new component-factory-based
plugin system (SPEC §4.5). A plugin declares its config schema and
registers component factories via ``register(ctx)``; the registration
context buffers factories and flushes them atomically on clean exit.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import inspect
import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from modex_agent.plugins.abc import ComponentFactory, ComponentSlot, PluginSource
from modex_agent.plugins.registry import ComponentRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "Plugin",
    "PluginRegistrationContext",
    "PluginDiscoveryConfig",
    "ComponentRegistryLoader",
]


# ---------------------------------------------------------------------------
# Plugin ABC
# ---------------------------------------------------------------------------


class Plugin(ABC):
    """Typed plugin entry point.

    A plugin declares its config schema (``config_model``) and registers
    component factories via ``register(ctx)``. The registration context
    buffers factories and flushes them atomically on clean exit
    (SPEC §4.5).

    Subclasses MUST set ``config_model`` (a frozen Pydantic ``BaseModel``
    with ``extra="forbid"``) and implement ``register()``.
    """

    config_model: ClassVar[type[BaseModel]]
    api_version: ClassVar[int] = 1

    @abstractmethod
    def register(self, ctx: PluginRegistrationContext) -> None:
        """Register component factories into *ctx*.

        Called by ``ComponentRegistryLoader`` during startup. The context
        manager handles atomicity: if this method raises, all buffered
        factories are discarded (no half-registration).
        """
        ...


# ---------------------------------------------------------------------------
# PluginRegistrationContext — collecting facade + context manager
# ---------------------------------------------------------------------------


class PluginRegistrationContext:
    """Collecting facade + context manager for plugin registration.

    Each ``register_*`` method buffers a ``(slot, name, factory)`` tuple
    into an internal list. On clean ``__exit__`` (or an explicit
    :meth:`flush`), all buffered factories are flushed to the registry.
    On exception from ``register()``, the buffer is discarded — atomicity
    guarantees no half-registration from a failing plugin (SPEC §4.5).

    ``source`` attributes the registrations to a discovery source
    (a :class:`PluginSource` value). The flush is
    source-aware (SPEC §4.1): a same-source duplicate ``(slot, name)``
    raises ``ValueError`` (a packaging/config error); a cross-source
    duplicate is resolved by source priority — user > project >
    entry_points > bundled, nearest-to-user wins (SPEC §3.5 O2). A
    directly-registered entry (source ``None``) preempts any source.

    The 10 ``register_*`` methods map 1:1 to the 10 ``ComponentSlot``
    values.
    """

    def __init__(self, registry: ComponentRegistry, *, source: PluginSource | None = None) -> None:
        self._registry = registry
        self._source: PluginSource | None = source
        self._buffer: list[tuple[ComponentSlot, str, ComponentFactory]] = []

    def _add(self, slot: ComponentSlot, name: str, factory: ComponentFactory) -> None:
        self._buffer.append((slot, name, factory))

    # ---- 10 register_* methods (one per ComponentSlot) ----

    def register_tool(self, name: str, factory: ComponentFactory) -> None:
        self._add(ComponentSlot.TOOL, name, factory)

    def register_hook(self, name: str, factory: ComponentFactory) -> None:
        self._add(ComponentSlot.HOOK, name, factory)

    def register_memory_system(self, name: str, factory: ComponentFactory) -> None:
        self._add(ComponentSlot.MEMORY_SYSTEM, name, factory)

    def register_provider(self, name: str, factory: ComponentFactory) -> None:
        self._add(ComponentSlot.LLM_PROVIDER, name, factory)

    def register_prompt_provider(self, name: str, factory: ComponentFactory) -> None:
        self._add(ComponentSlot.SYSTEM_PROMPT_PROVIDER, name, factory)

    def register_interceptor(self, name: str, factory: ComponentFactory) -> None:
        self._add(ComponentSlot.INTERCEPTOR, name, factory)

    def register_command(self, name: str, factory: ComponentFactory) -> None:
        self._add(ComponentSlot.COMMAND_HANDLER, name, factory)

    def register_execution_strategy(self, name: str, factory: ComponentFactory) -> None:
        self._add(ComponentSlot.EXECUTION_STRATEGY, name, factory)

    def register_input_stage(self, name: str, factory: ComponentFactory) -> None:
        self._add(ComponentSlot.INPUT_STAGE, name, factory)

    def register_namespace(self, name: str, factory: ComponentFactory) -> None:
        self._add(ComponentSlot.DATA_NAMESPACE, name, factory)

    # ---- context manager protocol ----

    def __enter__(self) -> PluginRegistrationContext:
        return self

    def __exit__(self, *exc: object) -> None:
        if exc[0] is None:
            self.flush()

    def flush(self) -> None:
        """Flush all buffered factories to the registry.

        Called on clean context exit, or explicitly by the loader (which
        isolates ``register()`` failures itself and lets a same-source
        conflict propagate out of ``load()``). Idempotent: the buffer is
        drained first, so a plugin that used ``with ctx:`` inside
        ``register()`` followed by the loader's explicit flush is a no-op
        on the second call. Two-phase per SPEC §4.1:

        - Phase 1 (validate, no mutation): every buffered entry is checked
          against the registry AND against the rest of the buffer — a
          same-source ``(slot, name)`` duplicate raises ``ValueError``
          before anything is registered, so a conflict never leaves a
          half-flushed plugin.
        - Phase 2 (apply): name absent → register (attributed to this
          context's source); name present → resolved by source priority
          (SPEC §3.5 O2): an entry from a lower-priority source is
          overwritten via ``register(overwrite=True)`` + info log (the
          entry's attribution moves to the new source); an entry from a
          higher-priority source is skipped + info log; an entry
          registered directly (source ``None``, on either side) is
          skipped + warning — direct registrations bypass source
          semantics.
        """
        buffer = self._buffer
        self._buffer = []
        if not buffer:
            return

        seen_in_buffer: set[tuple[ComponentSlot, str]] = set()
        for slot, name, _factory in buffer:
            key = (slot, name)
            existing = self._registry.registration_source(slot, name)
            in_buffer_dup = key in seen_in_buffer
            seen_in_buffer.add(key)
            if self._source is not None and (existing == self._source or in_buffer_dup):
                raise ValueError(
                    f"Component {name!r} in slot {slot.value!r} registered "
                    f"twice from source {self._source.value!r} (SPEC §4.1 "
                    "same-source conflict)"
                )

        for slot, name, factory in buffer:
            if name not in self._registry.names(slot):
                self._registry.register(slot, name, factory, source=self._source)
                continue
            existing = self._registry.registration_source(slot, name)
            if existing is None or self._source is None:
                logger.warning(
                    "component %r in slot %r already registered, "
                    "skipping (direct registration, no source priority)",
                    name,
                    slot.value,
                )
                continue
            if (
                PluginSource.SOURCE_PRIORITY[self._source]
                > PluginSource.SOURCE_PRIORITY[existing]
            ):
                self._registry.register(
                    slot, name, factory, source=self._source, overwrite=True
                )
                logger.info(
                    "component %r in slot %r overridden by higher-priority "
                    "source: %s -> %s",
                    name,
                    slot.value,
                    existing.value,
                    self._source.value,
                )
            else:
                logger.info(
                    "component %r in slot %r already registered from "
                    "higher-priority source %s, skipping (source %s)",
                    name,
                    slot.value,
                    existing.value,
                    self._source.value,
                )


# ---------------------------------------------------------------------------
# PluginDiscoveryConfig — frozen dataclass (rule 11: leaf value object)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginDiscoveryConfig:
    """Typed discovery configuration for ``ComponentRegistryLoader``.

    Drives all plugin discovery — no implicit directory guessing. The
    loader processes sources in a fixed order (bundled, project, user,
    entry_points); cross-source name conflicts are resolved by source
    priority, not processing order — user > project > entry_points >
    bundled (SPEC §3.5 O2).

    Frozen dataclass (rule 11) — a leaf value object with no behavior.
    Holds ``Plugin`` instances and ``Path`` objects which are not
    Pydantic-serializable, so frozen dataclass is preferred over
    Pydantic ``BaseModel`` (rule 11 vs rule 10 — this config does not
    cross serialization boundaries).
    """

    bundled_factories: tuple[Plugin, ...]
    project_plugin_paths: tuple[Path, ...]
    user_plugin_path: Path | None = None
    entry_point_group: str = "modex_agent.plugins"


# ---------------------------------------------------------------------------
# ComponentRegistryLoader — startup loader
# ---------------------------------------------------------------------------


class ComponentRegistryLoader:
    """Startup loader — discovers plugins and registers their factories.

    Cross-source name conflicts resolve by source priority: user >
    project > entry_points > bundled (SPEC §3.5 O2) — a higher-priority
    source overrides, a lower-priority one is skipped (info log).

    Fault isolation: one plugin failure (instantiation or ``register()``)
    logs an error and continues to the next plugin. A same-source
    duplicate ``(slot, name)`` is NOT isolated — it raises ``ValueError``
    out of :meth:`load` (SPEC §4.1: same-source conflicts are config
    errors).

    Atomicity per plugin: if ``register()`` raises, all buffered
    factories for that plugin are discarded — no half-registration.
    """

    @classmethod
    async def load(
        cls,
        registry: ComponentRegistry,
        discovery: PluginDiscoveryConfig,
    ) -> None:
        """Load all plugins from the configured discovery sources.

        Processes sources in priority order. Each plugin is wrapped in
        a ``PluginRegistrationContext`` for atomicity and a try/except
        for fault isolation.
        """
        # 1. Bundled (already-instantiated Plugin instances)
        for plugin in discovery.bundled_factories:
            cls._register_one(registry, plugin, source=PluginSource.BUNDLED)

        # 2. Project directories
        for path in discovery.project_plugin_paths:
            cls._load_from_directory(registry, path, source=PluginSource.PROJECT)

        # 3. User directory (optional)
        if discovery.user_plugin_path is not None:
            cls._load_from_directory(
                registry, discovery.user_plugin_path, source=PluginSource.USER
            )

        # 4. Entry points (PyPI)
        for plugin_cls in cls._discover_entry_points(discovery.entry_point_group):
            try:
                plugin = plugin_cls()
            except Exception as e:
                logger.error(
                    "Plugin %s from entry_points failed to instantiate: %s",
                    plugin_cls.__name__,
                    e,
                )
                continue
            cls._register_one(registry, plugin, source=PluginSource.ENTRY_POINTS)

    # ---- internal helpers ----

    @classmethod
    def _register_one(
        cls,
        registry: ComponentRegistry,
        plugin: Plugin,
        *,
        source: PluginSource,
    ) -> None:
        """Register one plugin instance.

        Fault isolation covers ``plugin.register()`` only: its exceptions
        are logged and the plugin's buffered factories are discarded
        (atomicity — no half-registration). The flush itself is NOT
        fault-isolated: a same-source duplicate (SPEC §4.1) raises
        ``ValueError`` out of ``load()`` so the conflicting source is
        fixed at boot instead of being silently shadowed.
        """
        ctx = PluginRegistrationContext(registry, source=source)
        try:
            plugin.register(ctx)
        except Exception as e:
            logger.error(
                "Plugin %s from %s failed: %s",
                type(plugin).__name__,
                source,
                e,
            )
            return
        ctx.flush()

    @classmethod
    def _load_from_directory(
        cls,
        registry: ComponentRegistry,
        directory: Path,
        *,
        source: PluginSource,
    ) -> None:
        """Scan *directory* for .py files, import Plugin subclasses.

        Non-existent or non-directory paths log a warning and return.
        Each .py file is imported under a deterministic per-file module
        name (see :meth:`_import_plugin_classes`); concrete (non-abstract)
        Plugin subclasses are instantiated and registered.
        """
        if not directory.exists():
            logger.warning("Plugin directory does not exist: %s", directory)
            return

        if not directory.is_dir():
            logger.warning("Plugin path is not a directory: %s", directory)
            return

        for py_file in sorted(directory.glob("*.py")):
            if py_file.name == "__init__.py":
                continue
            plugin_classes = cls._import_plugin_classes(py_file)
            for plugin_cls in plugin_classes:
                try:
                    plugin = plugin_cls()
                except Exception as e:
                    logger.error(
                        "Plugin %s from %s failed to instantiate: %s",
                        plugin_cls.__name__,
                        source,
                        e,
                    )
                    continue
                cls._register_one(registry, plugin, source=source)

    @classmethod
    def _import_plugin_classes(cls, py_file: Path) -> list[type[Plugin]]:
        """Import a .py file and return concrete Plugin subclasses.

        The module name is DETERMINISTIC — derived from the resolved file
        path (sha1 prefix) — so the same file discovered twice (re-scan,
        path listed twice, overlapping project dirs) maps to ONE
        ``sys.modules`` entry: the second discovery reuses the
        already-executed module instead of executing it again under a new
        name. This keeps Plugin class identity stable (``isinstance`` /
        ``issubclass``) and stops each scan from leaking a fresh module
        entry. The ``isinstance`` and ``issubclass`` checks are justified
        at this extension boundary — dynamic module loading for plugin
        discovery requires inspecting loaded types (rule 9).
        """
        resolved = py_file.resolve()
        module_name = (
            f"_modex_discovered_{hashlib.sha1(str(resolved).encode()).hexdigest()[:16]}"
        )
        cached = sys.modules.get(module_name)
        if cached is not None:
            module = cached
        else:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                logger.warning("Cannot load plugin module from %s", py_file)
                return []

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception as e:
                logger.error("Failed to execute plugin module %s: %s", py_file, e)
                return []

        result: list[type[Plugin]] = []
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            # isinstance + issubclass justified: extension boundary
            # (dynamic plugin discovery from loaded modules).
            if (
                isinstance(obj, type)
                and issubclass(obj, Plugin)
                and obj is not Plugin
                and not inspect.isabstract(obj)
            ):
                result.append(obj)
        return result

    @classmethod
    def _discover_entry_points(cls, group: str) -> list[type[Plugin]]:
        """Discover Plugin classes from PyPI entry points.

        Entry points in *group* should point to Plugin subclasses (e.g.,
        ``my_plugin = my_package:MyPlugin`` where ``MyPlugin`` is a
        ``Plugin`` subclass). ``ep.load()`` returns the class; the
        loader instantiates it.

        Returns an empty list if entry_points() is unavailable or no
        Plugin subclasses are found.
        """
        result: list[type[Plugin]] = []
        try:
            eps = importlib.metadata.entry_points()
            group_eps = eps.select(group=group)

            for ep in group_eps:
                try:
                    obj = ep.load()
                    # isinstance + issubclass justified: extension
                    # boundary (entry point plugin discovery).
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, Plugin)
                        and obj is not Plugin
                        and not inspect.isabstract(obj)
                    ):
                        result.append(obj)
                    else:
                        logger.warning(
                            "Entry point %s in group %s is not a Plugin subclass: %r",
                            ep.name,
                            group,
                            obj,
                        )
                except Exception as e:
                    logger.error(
                        "Entry point %s in group %s failed to load: %s",
                        ep.name,
                        group,
                        e,
                    )
        except Exception as e:
            # entry_points() itself may fail in some environments — the
            # loader continues with the remaining discovery sources.
            logger.warning(
                "Entry point discovery for plugin group %s failed: %s", group, e
            )

        return result
