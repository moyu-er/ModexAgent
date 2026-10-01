"""AppService — the application-service lifecycle skeleton (W4b).

The framework owns the generic assembly lifecycle building blocks every
application service shares:

- the three-root assembly (:class:`~modex_agent.app.roots.AppAssemblyRoots`),
- the component-registry load (``DefaultPlugin`` + the deployment's plugin
  directory under ``roots.plugins_dir``),
- the shared SQLite persistence open pair (registry DB BEFORE workspace
  materialization; home DB) and the strict shutdown tail that closes them
  LAST (pool routing store → home persistence → registry persistence),
- the shared session→pool routing store construction, and
- the lifecycle contract itself (:meth:`initialize` / :meth:`start` /
  :meth:`stop` + the cooperative ``_shutdown_event``) that the process
  supervisor (:mod:`modex_agent.app.supervisor`) drives.

A deployment subclasses this with its own orchestration: the concrete
``initialize``/``stop`` interleaving (model services, MCP wiring, the
workspace stack product, dynamic-workspace registration, presentation
hooks) is deployment-owned because those steps read deployment config
shapes and produce deployment-typed resources. The subclass composes the
generic blocks below in its own step order, keeping the shutdown side on
:meth:`_close_shared_persistence` so the last-to-close contract cannot
drift per deployment.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.adapters.output import OutputAdapter
from modex_agent.app.roots import AppAssemblyRoots
from modex_agent.core.emitter import ContentEmitter
from modex_agent.pipeline.adapters import InputAdapter
from modex_agent.plugins.loader import ChannelAdapterRegistry
from modex_agent.scope.component_registry import ComponentRegistry

if TYPE_CHECKING:
    from modex_agent.app.config import AppConfig
    from modex_agent.core.stores import PoolRoutingStore
    from modex_agent.persistence.managers import (
        RegistryPersistenceManager,
        WorkspacePersistenceManager,
    )

logger = logging.getLogger(__name__)

__all__ = ["AppService"]


class AppService(ABC):
    """Framework lifecycle skeleton for one application assembly.

    Owns the roots resolution (config/resource/workspace-home), the shared
    component registry, the persistence open/close pair, and the pool
    routing store. Deployment subclasses add their own assembly steps and
    call the generic blocks from them.
    """

    def __init__(
        self,
        config_dir: Path,
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        emitter_factory: Callable[[str, str], ContentEmitter[Any]],
        *,
        roots: AppAssemblyRoots | None = None,
        resource_root: Path | None = None,
        channel_adapters: ChannelAdapterRegistry | None = None,
    ) -> None:
        if roots is not None and roots.config_dir != config_dir.resolve():
            raise ValueError(
                f"roots.config_dir ({roots.config_dir}) does not match the "
                f"config_dir argument ({config_dir.resolve()})"
            )
        if roots is None:
            if resource_root is None:
                raise ValueError(
                    "either roots or resource_root must be provided "
                    "(the resident identity needs a resource root)"
                )
            roots = AppAssemblyRoots.resident(
                config_dir=config_dir, resource_root=resource_root
            )
        self.roots = roots
        self.config_dir = self.roots.config_dir
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter
        self.emitter_factory = emitter_factory

        # Service-level channel-adapter registry (create-or-accept): plugin
        # channel registrations from the generic load land here instead of
        # being dropped. A deployment that resolves adapters before the
        # registry load (its own construction order) passes its instance in.
        self._channel_adapter_registry = (
            channel_adapters if channel_adapters is not None else ChannelAdapterRegistry()
        )

        # The loaded application config (None until the deployment's config
        # step fills it; subclasses may pre-load and pass it in).
        self._app_config: AppConfig | None = None

        # Component registry (loaded once before pool creation): the FW
        # defaults + the deployment plugins under roots.plugins_dir.
        self._component_registry: ComponentRegistry | None = None
        self._strategy_registry: Any = None

        # Registry-level SQLite persistence manager. Opened BEFORE workspace
        # materialization; closed LAST at stop() (the registry DB is the
        # last-to-close persistence layer). None on the FILE backend or
        # before initialize().
        self._registry_persistence: RegistryPersistenceManager | None = None
        self._home_persistence: WorkspacePersistenceManager | None = None

        # Service-level session→pool routing store, shared across workspaces.
        self._pool_session_store: PoolRoutingStore | None = None

        # Deployment-owned workspace stack (the subclass fills this with its
        # wiring product; the framework only requires the quiesce/evict
        # contract through the subclass's own steps).
        self.workspace_stack: Any = None
        self.workspace_context: Any = None
        self._home_resources: Any = None
        self._pools: dict[str, Any] = {}
        self.pool_router: Any = None

        # Runtime control.
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ── Lifecycle contract (deployment orchestrates the step order) ──

    @abstractmethod
    async def initialize(self) -> None:
        """Assemble the application (config → declaration → registry →
        persistence → workspace stack → home materialization)."""

    @abstractmethod
    async def start(self) -> None:
        """Run until the cooperative ``_shutdown_event`` is set."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down in the strict order; shared persistence closes last."""

    def request_shutdown(self) -> None:
        """Cooperative shutdown trigger (signal handlers call this)."""
        self._shutdown_event.set()

    # ── Generic assembly blocks ─────────────────────────────────────────

    async def _load_component_registry(self) -> ComponentRegistry:
        """Load ``DefaultPlugin`` (bundled FW defaults) + the deployment's
        plugins from ``roots.plugins_dir`` + the per-user plugin directory
        (default-on, opt-out via app-config ``user_plugins_enabled``)
        into a fresh registry.

        Loaded once at service level so all pools share the same factory
        set. A missing user directory is normal (no user plugins) — the
        loader logs at debug.
        """
        from modex_agent.plugins.defaults import DefaultPlugin
        from modex_agent.plugins.loader import (
            DEFAULT_USER_PLUGIN_DIR,
            ComponentRegistryLoader,
            PluginDiscoveryConfig,
        )

        user_plugins_enabled = (
            self._app_config is None or self._app_config.user_plugins_enabled
        )
        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(),),
                project_plugin_paths=(self.roots.plugins_dir,),
                user_plugin_path=DEFAULT_USER_PLUGIN_DIR if user_plugins_enabled else None,
                channel_adapters=self._channel_adapter_registry,
            ),
        )
        logger.info("Component registry: %s", self.roots.plugins_dir)
        return registry

    async def _open_shared_persistence(self, app_config: AppConfig) -> None:
        """Open the registry DB BEFORE workspace materialization (the
        registry store is ready when workspaces start using it), then the
        home workspace DB. No-op on the FILE backend."""
        from modex_agent.persistence.config import PersistenceBackend
        from modex_agent.persistence.managers import (
            RegistryPersistenceManager,
            WorkspacePersistenceManager,
        )

        if app_config.persistence.backend is PersistenceBackend.SQLITE:
            registry_db_path = self.roots.registry_db_path(
                app_config.paths.data_dir_name
            )
            self._registry_persistence = RegistryPersistenceManager(registry_db_path)
            await self._registry_persistence.open()

            home_db_path = self.roots.home_db_path(app_config.paths.data_dir_name)
            self._home_persistence = WorkspacePersistenceManager(home_db_path)
            await self._home_persistence.open()

    async def _build_pool_session_store(self, app_config: AppConfig) -> PoolRoutingStore:
        """The shared session→pool routing store (service-wide singleton)."""
        from modex_agent.plugins.assembly.backend_factory import build_pool_routing_store
        from modex_agent.workspace.paths import WORKSPACE_STATE_DB

        home_data_dir = self.roots.home_data_dir(app_config.paths.data_dir_name)
        return build_pool_routing_store(
            app_config,
            self._home_persistence,
            data_dir=home_data_dir,
            db_path=home_data_dir / WORKSPACE_STATE_DB,
        )

    async def _close_shared_persistence(self) -> None:
        """The strict shutdown tail: pool routing store → home persistence →
        registry persistence (the registry DB is the global, last-to-close
        persistence layer). Each step is best-effort (suppressed) so the
        order cannot be cut short by a failing close. Idempotent."""
        import contextlib

        if self._pool_session_store is not None:
            with contextlib.suppress(BaseException):
                self._pool_session_store.close()
            self._pool_session_store = None
        if self._home_persistence is not None:
            with contextlib.suppress(BaseException):
                await self._home_persistence.close()
            self._home_persistence = None
        if self._registry_persistence is not None:
            with contextlib.suppress(BaseException):
                await self._registry_persistence.close()
            self._registry_persistence = None
