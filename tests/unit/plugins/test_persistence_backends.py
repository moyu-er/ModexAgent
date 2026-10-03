"""W2b — persistence-backend bundles + the registration face.

Covers the two bundled bundles producing today's store classes per family,
the process-level registry (bundled defaults seeded, a custom plugin bundle
under a new name resolving via the config string, unknown names raising
with the registered options), the plugin registration plumbing through
``ComponentRegistryLoader.load`` and the app-service load, the
manager-opening pair, the app-service manager gate, and the config face
(``PersistenceConfig.backend`` is the registry-name string — YAML
``file``/``sqlite`` keep working).
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

import modex_agent.plugins.loader as loader_module
from modex_agent.app.config import AppConfig
from modex_agent.app.service import AppService
from modex_agent.core.backend_registry import BackendRegistry
from modex_agent.core.scope import RecordScope
from modex_agent.persistence.config import PersistenceConfig
from modex_agent.persistence.managers import (
    RegistryPersistenceManager,
    WorkspacePersistenceManager,
)
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.plugins.persistence_backends import (
    FILE_BACKEND_NAME,
    SQLITE_BACKEND_NAME,
    FilePersistenceBackendBundle,
    PersistenceBackendBundle,
    SqlitePersistenceBackendBundle,
    persistence_backend_registry,
    resolve_persistence_backend,
)
from modex_agent.scope.component_registry import ComponentRegistry


class _EmptyConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


class _StubBundle(SqlitePersistenceBackendBundle):
    """A named third-party bundle: every member is inherited — the
    resolution road is the test subject, not store construction."""


class _PersistenceBackendPlugin(Plugin):
    """Registers one named persistence backend (the third-party shape)."""

    config_model = _EmptyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_persistence_backend("stub-persistence", _StubBundle)


def _app_config(backend: str) -> AppConfig:
    return AppConfig.model_validate({"persistence": {"backend": backend}})


# ── Bundled bundles produce today's store classes ───────────────────────────


class TestFileBundleProducesTodaysClasses:
    def test_inbox(self, tmp_path: Path) -> None:
        from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ

        inbox = FilePersistenceBackendBundle().build_inbox(
            None, tmp_path / "inbox", tmp_path / "state.db", RecordScope()
        )
        assert isinstance(inbox, LocalFileInboxMQ)

    def test_turn_state_store(self, tmp_path: Path) -> None:
        from modex_agent.runtime.store import JsonFileTurnStateStore

        store = FilePersistenceBackendBundle().build_turn_state_store(
            None, tmp_path / "turns", MagicMock()
        )
        assert isinstance(store, JsonFileTurnStateStore)

    def test_session_store(self, tmp_path: Path) -> None:
        from modex_agent.persistence.adapters.pool_session_store import (
            WorkspacePoolSessionStore,
        )

        store = FilePersistenceBackendBundle().build_session_store(
            None, tmp_path, lambda _info: "main", ".modex"
        )
        assert isinstance(store, WorkspacePoolSessionStore)

    def test_pool_routing_store(self, tmp_path: Path) -> None:
        from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore

        store = FilePersistenceBackendBundle().build_pool_routing_store(
            None, tmp_path, tmp_path / "state.db"
        )
        assert isinstance(store, LocalFilePoolRoutingStore)

    def test_external_session_map_store(self, tmp_path: Path) -> None:
        from modex_agent.agents.external.session_store import (
            LocalFileExternalSessionMapStore,
        )

        store = FilePersistenceBackendBundle().build_external_session_map_store(
            None, tmp_path, RecordScope()
        )
        assert isinstance(store, LocalFileExternalSessionMapStore)

    def test_memory_registry_is_none(self, tmp_path: Path) -> None:
        assert (
            FilePersistenceBackendBundle().build_memory_registry(
                None, tmp_path, RecordScope()
            )
            is None
        )

    def test_workspace_registry_store(self, tmp_path: Path) -> None:
        from modex_agent.workspace.store import GlobalWorkspaceStore

        store = FilePersistenceBackendBundle().build_workspace_registry_store(
            None, tmp_path, ".modex"
        )
        assert isinstance(store, GlobalWorkspaceStore)

    def test_approval_audit_store_is_none(self) -> None:
        assert (
            FilePersistenceBackendBundle().build_approval_audit_store(None, RecordScope())
            is None
        )

    def test_session_tree_stores(self, tmp_path: Path) -> None:
        from modex_agent.multi_agent.session_tree import (
            LocalFileMessageTrackStore,
            LocalFileSessionTreeStore,
            LocalFileTreeNodeStore,
        )

        tree, node, track = FilePersistenceBackendBundle().build_session_tree_stores(
            None, tmp_path, RecordScope()
        )
        assert isinstance(tree, LocalFileSessionTreeStore)
        assert isinstance(node, LocalFileTreeNodeStore)
        assert isinstance(track, LocalFileMessageTrackStore)


class TestSqliteBundleProducesTodaysClasses:
    async def test_workspace_families(self, tmp_path: Path) -> None:
        from modex_agent.memory.registry import HybridMemoryStoreRegistry
        from modex_agent.persistence.adapters.approval_audit_store import (
            SqliteApprovalAuditStore,
        )
        from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ
        from modex_agent.persistence.adapters.session_store import SqliteSessionStore
        from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore

        manager = WorkspacePersistenceManager(tmp_path / "state.db")
        await manager.open()
        try:
            bundle = SqlitePersistenceBackendBundle()
            assert isinstance(
                bundle.build_inbox(
                    manager, tmp_path / "inbox", tmp_path / "state.db", RecordScope()
                ),
                SqliteInboxMQ,
            )
            assert isinstance(
                bundle.build_turn_state_store(manager, tmp_path / "turns", MagicMock()),
                SqliteTurnStateStore,
            )
            assert isinstance(
                bundle.build_session_store(
                    manager, tmp_path, lambda _info: "main", ".modex"
                ),
                SqliteSessionStore,
            )
            assert isinstance(
                bundle.build_memory_registry(manager, tmp_path, RecordScope()),
                HybridMemoryStoreRegistry,
            )
            assert isinstance(
                bundle.build_approval_audit_store(manager, RecordScope()),
                SqliteApprovalAuditStore,
            )
        finally:
            await manager.close()

    def test_workspace_registry_store_returns_registry_manager_store(
        self, tmp_path: Path
    ) -> None:
        registry_manager = MagicMock(spec=RegistryPersistenceManager)
        store = SqlitePersistenceBackendBundle().build_workspace_registry_store(
            registry_manager, tmp_path, ".modex"
        )
        assert store is registry_manager.store

    async def test_session_tree_stores(self, tmp_path: Path) -> None:
        from modex_agent.multi_agent.session_tree import (
            SqliteMessageTrackStore,
            SqliteSessionTreeStore,
            SqliteTreeNodeStore,
        )

        manager = WorkspacePersistenceManager(tmp_path / "state.db")
        await manager.open()
        try:
            tree, node, track = (
                SqlitePersistenceBackendBundle().build_session_tree_stores(
                    manager, tmp_path, RecordScope()
                )
            )
            assert isinstance(tree, SqliteSessionTreeStore)
            assert isinstance(node, SqliteTreeNodeStore)
            assert isinstance(track, SqliteMessageTrackStore)
        finally:
            await manager.close()

    def test_missing_workspace_manager_falls_back_to_file(self, tmp_path: Path) -> None:
        from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ

        store = SqlitePersistenceBackendBundle().build_inbox(
            None, tmp_path, tmp_path / "state.db", RecordScope()
        )
        assert isinstance(store, LocalFileInboxMQ)

    def test_missing_registry_manager_falls_back_to_file(self, tmp_path: Path) -> None:
        from modex_agent.workspace.store import GlobalWorkspaceStore

        store = SqlitePersistenceBackendBundle().build_workspace_registry_store(
            None, tmp_path, ".modex"
        )
        assert isinstance(store, GlobalWorkspaceStore)


# ── Registry resolution + the config face ───────────────────────────────────


class TestRegistryResolution:
    def test_singleton_seeds_both_bundled_backends(self) -> None:
        registry = persistence_backend_registry()

        assert FILE_BACKEND_NAME in registry.names()
        assert SQLITE_BACKEND_NAME in registry.names()
        assert isinstance(
            registry.resolve(FILE_BACKEND_NAME), FilePersistenceBackendBundle
        )
        assert isinstance(
            registry.resolve(SQLITE_BACKEND_NAME), SqlitePersistenceBackendBundle
        )

    def test_none_config_resolves_the_file_bundle(self) -> None:
        assert isinstance(resolve_persistence_backend(None), FilePersistenceBackendBundle)

    def test_config_string_resolves_the_matching_bundle(self) -> None:
        assert isinstance(
            resolve_persistence_backend(_app_config("file")),
            FilePersistenceBackendBundle,
        )
        assert isinstance(
            resolve_persistence_backend(_app_config("sqlite")),
            SqlitePersistenceBackendBundle,
        )

    def test_custom_bundle_resolves_via_config_string(self) -> None:
        persistence_backend_registry().register("stub-persistence", _StubBundle)

        bundle = resolve_persistence_backend(_app_config("stub-persistence"))

        assert isinstance(bundle, _StubBundle)

    def test_unknown_config_name_raises_listing_options(self) -> None:
        registry = BackendRegistry[PersistenceBackendBundle](
            family="persistence backend"
        )
        registry.register(FILE_BACKEND_NAME, FilePersistenceBackendBundle)

        with pytest.raises(ValueError, match="no-such-backend") as excinfo:
            registry.resolve("no-such-backend")
        assert FILE_BACKEND_NAME in str(excinfo.value)

        with pytest.raises(ValueError, match="no-such-backend"):
            resolve_persistence_backend(_app_config("no-such-backend"))


class TestConfigFace:
    def test_default_backend_is_sqlite(self) -> None:
        assert PersistenceConfig().backend == "sqlite"

    def test_backend_accepts_bundled_name_strings(self) -> None:
        assert PersistenceConfig(backend="file").backend == "file"
        assert PersistenceConfig.model_validate({"backend": "sqlite"}).backend == "sqlite"
        assert (
            AppConfig.model_validate(
                {"persistence": {"backend": "file"}}
            ).persistence.backend
            == "file"
        )

    def test_backend_is_an_open_name_string(self) -> None:
        """The registry owns the closed set — the config carries any name and
        resolution fails loudly on unregistered ones (the plugin face adds
        backends without config-schema changes)."""
        assert PersistenceConfig(backend="third-party").backend == "third-party"


# ── Registration plumbing through the loader ────────────────────────────────


async def test_plugin_persistence_backend_lands_in_attached_registry(
    tmp_path: Path,
) -> None:
    registry = BackendRegistry[PersistenceBackendBundle](family="persistence backend")

    await ComponentRegistryLoader.load(
        ComponentRegistry(),
        PluginDiscoveryConfig(
            bundled_factories=(_PersistenceBackendPlugin(),),
            project_plugin_paths=(),
            user_plugin_path=tmp_path / "absent",
            persistence_backends=registry,
        ),
    )

    assert "stub-persistence" in registry.names()
    assert isinstance(registry.resolve("stub-persistence"), _StubBundle)


async def test_plugin_persistence_backend_dropped_with_warning_without_registry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="modex_agent.plugins.loader"):
        await ComponentRegistryLoader.load(
            ComponentRegistry(),
            PluginDiscoveryConfig(
                bundled_factories=(_PersistenceBackendPlugin(),),
                project_plugin_paths=(),
                user_plugin_path=tmp_path / "absent",
            ),
        )

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("stub-persistence" in m and "persistence" in m for m in warnings), warnings


def test_register_persistence_backend_buffers_until_flush() -> None:
    ctx = PluginRegistrationContext(registry=None)

    ctx.register_persistence_backend("stub-persistence", _StubBundle)

    assert ctx.pending_persistence_backends() == ("stub-persistence",)


# ── Manager-opening pair + the app-service gate ─────────────────────────────


class TestManagerOpening:
    def test_file_bundle_opens_no_managers(self, tmp_path: Path) -> None:
        bundle = FilePersistenceBackendBundle()

        assert bundle.open_registry_manager(tmp_path / "registry.db") is None
        assert bundle.open_workspace_manager(tmp_path / "state.db") is None

    def test_sqlite_bundle_constructs_both_managers(self, tmp_path: Path) -> None:
        bundle = SqlitePersistenceBackendBundle()

        assert isinstance(
            bundle.open_registry_manager(tmp_path / "registry.db"),
            RegistryPersistenceManager,
        )
        assert isinstance(
            bundle.open_workspace_manager(tmp_path / "state.db"),
            WorkspacePersistenceManager,
        )


class _SkeletonService(AppService):
    """The abstract lifecycle steps are irrelevant to the persistence gate."""

    async def initialize(self) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def _skeleton(tmp_path: Path) -> _SkeletonService:
    return _SkeletonService(
        config_dir=tmp_path / "config",
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        emitter_factory=MagicMock(),
        resource_root=tmp_path,
    )


class TestServiceManagerGate:
    async def test_file_backend_opens_nothing(self, tmp_path: Path) -> None:
        service = _skeleton(tmp_path)

        await service._open_shared_persistence(_app_config("file"))  # noqa: SLF001 — test seam

        assert service._registry_persistence is None  # noqa: SLF001
        assert service._home_persistence is None  # noqa: SLF001

    async def test_sqlite_backend_opens_both_managers(self, tmp_path: Path) -> None:
        service = _skeleton(tmp_path)

        await service._open_shared_persistence(_app_config("sqlite"))  # noqa: SLF001 — test seam

        try:
            assert isinstance(  # noqa: SLF001
                service._registry_persistence, RegistryPersistenceManager
            )
            assert isinstance(  # noqa: SLF001
                service._home_persistence, WorkspacePersistenceManager
            )
        finally:
            await service._close_shared_persistence()  # noqa: SLF001


_PROJECT_PLUGIN_SOURCE = '''\
from pydantic import BaseModel

from modex_agent.plugins.loader import Plugin, PluginRegistrationContext
from modex_agent.plugins.persistence_backends import SqlitePersistenceBackendBundle


class _PluginConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


class _ProjectBundle(SqlitePersistenceBackendBundle):
    pass


class ProjectBackendPlugin(Plugin):
    config_model = _PluginConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_persistence_backend("project-backend", _ProjectBundle)
'''


async def test_service_registry_load_lands_project_bundle_in_process_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end third-party road: a project plugin's persistence
    bundle lands in the process-level registry the assembly factories and
    the service manager gate resolve through."""
    monkeypatch.setattr(loader_module, "DEFAULT_USER_PLUGIN_DIR", tmp_path / "absent")
    plugins_dir = tmp_path / "bot_plugins"
    plugins_dir.mkdir()
    (plugins_dir / "project_backend_plugin.py").write_text(
        _PROJECT_PLUGIN_SOURCE, encoding="utf-8"
    )
    service = _skeleton(tmp_path)

    await service._load_component_registry()  # noqa: SLF001 — test seam

    registry = persistence_backend_registry()
    assert "project-backend" in registry.names()
    bundle = resolve_persistence_backend(_app_config("project-backend"))
    assert type(bundle).__name__ == "_ProjectBundle"
