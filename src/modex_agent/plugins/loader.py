"""Plugin(ABC) + PluginRegistrationContext + PluginDiscoveryConfig
+ ComponentRegistryLoader.

Replaces the legacy injection bridge with the new component-factory-based
plugin system (SPEC §4.5). A plugin declares its config schema and
registers component factories via ``register(ctx)``; the registration
context buffers factories and flushes them atomically on clean exit.

Besides the 11 compile-time component slots, the context carries a
service-level channel-adapter registration face
(:meth:`PluginRegistrationContext.register_channel_adapter`) — channel
adapters are resolved once per service boot from config, not compiled
into assembly specs, so they land in a dedicated
:class:`ChannelAdapterRegistry` instead of a ``ComponentSlot``.

Packaging contract (W6 — the directory layout is NOT the API):

- **Project dir** — ``PluginDiscoveryConfig.project_plugin_paths``;
  the deployment's plugin package directory (e.g. the bot project's
  ``bot_plugins/``). Each ``*.py`` file in the directory is imported
  under a QUALIFIED synthetic name
  (``modex_agent_userplugins_<dir-sha>.<module>``) via importlib —
  no ``sys.path`` mutation, no top-level ``plugins`` package, no
  importable-name requirements on the directory. A plugin may also be
  an installed distribution exposing entry points in the
  ``modex_agent.plugins`` group (``ComponentRegistryLoader`` resolves
  them through :mod:`importlib.metadata`).
- **User dir** — ``PluginDiscoveryConfig.user_plugin_path``; enabled by
  default at ``DEFAULT_USER_PLUGIN_DIR`` (``~/.modex_agent/plugins``)
  by the app-service registry load, opt-out via the app-config flag
  ``user_plugins_enabled: false``. Same qualified loading as the
  project dir; a missing user dir is normal (debug log, not a warning).
- **Priority** — user > project > entry_points > bundled
  (SPEC §3.5 O2), resolved by the registration flush, not scan order.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import inspect
import logging
import sys
import types
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

from pydantic import BaseModel

from modex_agent.adapters.output import OutputAdapter
from modex_agent.core.emitter import ContentEmitter
from modex_agent.pipeline.adapters import InputAdapter
from modex_agent.scope.capability import Capability
from modex_agent.scope.component_registry import ComponentRegistry, PluginSource
from modex_agent.scope.components import ComponentFactory, ComponentSlot

logger = logging.getLogger(__name__)

__all__ = [
    "Plugin",
    "PluginRegistrationContext",
    "PluginDiscoveryConfig",
    "ComponentRegistryLoader",
    "ChannelAdapterRegistry",
    "ChannelBuildContext",
    "ChannelBuildResult",
    "ChannelAdapterFactory",
    "DEFAULT_USER_PLUGIN_DIR",
    "USER_PLUGIN_PACKAGE_PREFIX",
]


# ---------------------------------------------------------------------------
# Channel adapters — service-level registration (NOT a compile-time slot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelBuildContext:
    """Generic build context handed to a channel-adapter factory.

    ``raw_config`` is the open per-channel config payload (IM sections from
    the deployment's config files) — genuinely open/heterogeneous across
    channels, the sanctioned rule-3 exception.
    """

    config_dir: Path
    raw_config: Mapping[str, Any]


@dataclass(frozen=True)
class ChannelBuildResult:
    """The adapters + emitter factory one channel contributes."""

    input_adapter: InputAdapter
    output_adapter: OutputAdapter
    emitter_factory: Callable[[str, str], ContentEmitter[Any]]


#: A channel factory returns ``None`` when the channel is configured off.
ChannelAdapterFactory = Callable[[ChannelBuildContext], ChannelBuildResult | None]


class ChannelAdapterRegistry:
    """name → channel-adapter factory, populated via
    :meth:`PluginRegistrationContext.register_channel_adapter`.

    The service-level counterpart of the compile-time component slots:
    adapters resolve once per boot from config (``resolve``), never through
    scope compilation. Re-registering the same ``(name, factory)`` pair is
    idempotent (plugin modules are imported once under deterministic
    names); the same name with a DIFFERENT factory is a packaging error.
    """

    def __init__(self) -> None:
        self._factories: dict[str, ChannelAdapterFactory] = {}

    def register(self, name: str, factory: ChannelAdapterFactory) -> None:
        existing = self._factories.get(name)
        if existing is not None and existing is not factory:
            raise ValueError(
                f"channel adapter {name!r} registered twice with different "
                "factories (packaging/config error)"
            )
        self._factories[name] = factory

    def resolve(self, name: str) -> ChannelAdapterFactory:
        """The factory registered under *name* (the config-resolution face)."""
        try:
            return self._factories[name]
        except KeyError:
            raise ValueError(
                f"channel adapter {name!r} is not registered — no plugin "
                "contributed it to the channel-adapter registry"
            ) from None

    def names(self) -> tuple[str, ...]:
        """Registered channel names, in registration order."""
        return tuple(self._factories)


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

    The 11 ``register_*`` methods map 1:1 to the 11 ``ComponentSlot``
    values.
    """

    def __init__(
        self,
        registry: ComponentRegistry | None = None,
        *,
        source: PluginSource | None = None,
        channel_adapters: ChannelAdapterRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._source: PluginSource | None = source
        self._channel_adapters = channel_adapters
        # CAPABILITY entries are capability instances, not factories
        # (SPEC §4) — the one slot whose buffered object is not a
        # ComponentFactory.
        self._buffer: list[tuple[ComponentSlot, str, ComponentFactory | Capability]] = []
        self._channel_buffer: list[tuple[str, ChannelAdapterFactory]] = []

    def _add(
        self, slot: ComponentSlot, name: str, component: ComponentFactory | Capability
    ) -> None:
        if self._registry is None:
            raise ValueError(
                f"component {name!r} in slot {slot.value!r} cannot register: "
                "this PluginRegistrationContext carries no ComponentRegistry "
                "(channel-adapter-only contexts register channel adapters)"
            )
        self._buffer.append((slot, name, component))

    # ---- 11 register_* methods (one per ComponentSlot) ----

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

    def register_capability(self, name: str, capability: Capability) -> None:
        """Register a capability INSTANCE under ``(CAPABILITY, name)``.

        Unlike the 10 factory slots, the CAPABILITY slot stores the
        capability instance itself (SPEC §4) — capabilities participate
        in compilation, not per-assembly instantiation. Source priority
        and duplicate semantics are identical to the factory slots.
        """
        self._add(ComponentSlot.CAPABILITY, name, capability)

    def register_channel_adapter(self, name: str, factory: ChannelAdapterFactory) -> None:
        """Register a channel-adapter factory under *name* (service level).

        Channel adapters are NOT a compile-time component slot: they are
        resolved once per service boot from config through the
        :class:`ChannelAdapterRegistry` attached to this context. When the
        context carries no channel registry (e.g. a registry-only load),
        the entry is buffered and dropped at flush with a warning naming
        the plugin — channel registrations are owned by whichever load
        attached the registry.
        """
        self._channel_buffer.append((name, factory))

    def pending_channel_adapters(self) -> tuple[str, ...]:
        """Channel-adapter names buffered but not yet flushed.

        The loader's read face for the drop warning: a load that carries
        no :class:`ChannelAdapterRegistry` checks this after
        ``plugin.register()`` so the warning can name the plugin and its
        dropped adapters before :meth:`flush` drains the buffer.
        """
        return tuple(name for name, _factory in self._channel_buffer)

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
        channel_buffer = self._channel_buffer
        self._channel_buffer = []
        if channel_buffer and self._channel_adapters is not None:
            for name, channel_factory in channel_buffer:
                self._channel_adapters.register(name, channel_factory)
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

        for slot, name, component in buffer:
            # The CAPABILITY slot stores capability instances, not
            # factories (SPEC §4) — the registry's ComponentFactory-typed
            # store face predates the 11th slot. ``register`` stores the
            # object verbatim and ``resolve`` returns it unchanged, so
            # this cast is representation-only: a registered Capability
            # resolves to itself (identity preserved for the compile-time
            # consumer).
            factory = cast("ComponentFactory", component)
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
    channel_adapters: ChannelAdapterRegistry | None = None
    """Landing registry for plugin channel-adapter registrations. When
    ``None``, a plugin that registers channel adapters gets a WARNING and
    the registrations are dropped — channel registrations are only owned
    by whichever load attached a registry (the app-service boot does)."""


#: Default per-user plugin directory — enabled by default by the
#: app-service registry load; opt out with the app-config flag
#: ``user_plugins_enabled: false``. A missing directory is normal (no
#: user plugins installed), so its absence logs at debug, not warning.
DEFAULT_USER_PLUGIN_DIR = Path.home() / ".modex_agent" / "plugins"

#: Qualified-name prefix for directory-discovered plugin modules. Each
#: discovered directory becomes one synthetic parent package
#: (``modex_agent_userplugins_<dir-sha>``) whose ``__path__`` anchors the
#: directory, and each plugin file loads as its submodule — nothing lands
#: at the top level of ``sys.modules`` and no ``sys.path`` entry is
#: required.
USER_PLUGIN_PACKAGE_PREFIX = "modex_agent_userplugins"


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
            cls._register_one(
                registry, plugin, source=PluginSource.BUNDLED,
                channel_adapters=discovery.channel_adapters,
            )

        # 2. Project directories
        for path in discovery.project_plugin_paths:
            cls._load_from_directory(
                registry, path, source=PluginSource.PROJECT,
                channel_adapters=discovery.channel_adapters,
            )

        # 3. User directory (optional)
        if discovery.user_plugin_path is not None:
            cls._load_from_directory(
                registry, discovery.user_plugin_path, source=PluginSource.USER,
                channel_adapters=discovery.channel_adapters,
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
            cls._register_one(
                registry, plugin, source=PluginSource.ENTRY_POINTS,
                channel_adapters=discovery.channel_adapters,
            )

    # ---- internal helpers ----

    @classmethod
    def _register_one(
        cls,
        registry: ComponentRegistry,
        plugin: Plugin,
        *,
        source: PluginSource,
        channel_adapters: ChannelAdapterRegistry | None = None,
    ) -> None:
        """Register one plugin instance.

        Fault isolation covers ``plugin.register()`` only: its exceptions
        are logged and the plugin's buffered factories are discarded
        (atomicity — no half-registration). The flush itself is NOT
        fault-isolated: a same-source duplicate (SPEC §4.1) raises
        ``ValueError`` out of :meth:`load` so the conflicting source is
        fixed at boot instead of being silently shadowed.

        Channel-adapter registrations land in *channel_adapters* when the
        load carries one; without one they are dropped with a WARNING
        naming the plugin and its adapters (the generic load path's only
        channel face — service-level boots attach a registry).
        """
        ctx = PluginRegistrationContext(
            registry, source=source, channel_adapters=channel_adapters
        )
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
        if channel_adapters is None:
            dropped = ctx.pending_channel_adapters()
            if dropped:
                logger.warning(
                    "Plugin %s from %s registered channel adapters %s but "
                    "the load carries no ChannelAdapterRegistry — dropping "
                    "them (attach a registry via PluginDiscoveryConfig."
                    "channel_adapters to keep them)",
                    type(plugin).__name__,
                    source,
                    list(dropped),
                )
        ctx.flush()

    @classmethod
    def _load_from_directory(
        cls,
        registry: ComponentRegistry,
        directory: Path,
        *,
        source: PluginSource,
        channel_adapters: ChannelAdapterRegistry | None = None,
    ) -> None:
        """Scan *directory* for .py files, import Plugin subclasses.

        Non-existent paths log at debug (a missing plugin directory — the
        default-on user dir — means "no plugins there", not an error); a
        path that exists but is not a directory is a configuration
        mistake and warns. Each .py file is imported under a qualified
        name inside the directory's synthetic package (see
        :meth:`_import_plugin_classes`); concrete (non-abstract)
        ``Plugin`` subclasses are instantiated and registered.
        """
        if not directory.exists():
            logger.debug("Plugin directory does not exist: %s", directory)
            return

        if not directory.is_dir():
            logger.warning("Plugin path is not a directory: %s", directory)
            return

        package_name = cls._directory_package_name(directory)
        for py_file in sorted(directory.glob("*.py")):
            if py_file.name == "__init__.py":
                continue
            plugin_classes = cls._import_plugin_classes(py_file, package_name)
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
                cls._register_one(
                    registry, plugin, source=source, channel_adapters=channel_adapters
                )

    @classmethod
    def _directory_package_name(cls, directory: Path) -> str:
        """The deterministic synthetic package name for *directory*.

        Keyed on the RESOLVED directory path, so the same directory
        discovered twice — a re-scan, the path listed twice, or an alias
        (symlink) — maps to ONE package and its files to ONE
        ``sys.modules`` entry each.
        """
        digest = hashlib.sha1(str(directory.resolve()).encode()).hexdigest()[:16]
        return f"{USER_PLUGIN_PACKAGE_PREFIX}_{digest}"

    @classmethod
    def _import_plugin_classes(
        cls,
        py_file: Path,
        package_name: str,
    ) -> list[type[Plugin]]:
        """Import a .py file as ``<package_name>.<stem>`` and return
        concrete Plugin subclasses.

        The parent ``package_name`` module is anchored in ``sys.modules``
        with ``__path__`` pointing at the file's directory (created on
        first use), so the module name is QUALIFIED — nothing is
        materialized at the top level of ``sys.modules`` and no
        ``sys.path`` mutation is needed. Relative imports between plugin
        files in one directory resolve through the parent package's
        ``__path__``. Names are deterministic, so the same file
        discovered twice (re-scan, overlapping project dirs) reuses the
        already-executed module — Plugin class identity stays stable
        (``isinstance`` / ``issubclass``) and rescans do not leak module
        entries. The ``isinstance``/``issubclass`` checks are justified
        at this extension boundary — dynamic module loading for plugin
        discovery requires inspecting loaded types (rule 9).
        """
        resolved = py_file.resolve()
        parent = sys.modules.get(package_name)
        if parent is None:
            parent = types.ModuleType(package_name)
            parent.__path__ = [str(resolved.parent)]  # type: ignore[assignment]
            parent.__package__ = package_name
            sys.modules[package_name] = parent

        module_name = f"{package_name}.{resolved.stem}"
        cached = sys.modules.get(module_name)
        if cached is not None:
            module = cached
        else:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                logger.warning("Cannot load plugin module from %s", py_file)
                return []

            module = importlib.util.module_from_spec(spec)
            module.__package__ = package_name
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception as e:
                logger.error("Failed to execute plugin module %s: %s", py_file, e)
                del sys.modules[module_name]
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
