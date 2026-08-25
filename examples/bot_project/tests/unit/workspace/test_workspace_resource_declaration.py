"""Workspace resource-selection declaration (ticket 14) — split-brain baseline.

The migration replaces the BIZ wiring modules' resource selection with the
scope declaration's workspace layer (memory backend / path layout / MCP
server set / hosted pools). The equivalence contract: for equivalent
configs, the declaration-driven build lands data in EXACTLY the same
locations as today's config-driven (``bot_config.yml``) build.

This module carries the BASELINE (old road) first — a no-declaration
project whose ``AppConfig`` drives the backend/paths, with the landing
manifest frozen. The migration commit adds the declaration-driven builds
and asserts manifest equality against this frozen baseline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from bot.service.pool.declaration import (
    apply_workspace_resource_selection,
    load_scope_declaration_opt,
)
from bot.workspace.wiring.resources import _build_resources, _stop_resources

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_router import PoolRoutingStore
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.defaults.prompt import FilePromptProviderFactory
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext

_MINIMAL_DECL = """\
workspace:
  name: landing-test
  pools:
    main:
      agents:
        main:
          description: declared main agent
"""


def _write_project(
    project_dir: Path,
    *,
    declaration: str | None = None,
    persistence: str = "sqlite",
    data_dir_name: str = ".modex",
) -> None:
    """Write a one-pool (``main``) project tree with its scope declaration
    (the pool list source since ticket 11)."""
    (project_dir / "agents").mkdir(parents=True, exist_ok=True)
    (project_dir / "agents" / "main.md").write_text(
        "You are a helpful assistant. Reply briefly.\n", encoding="utf-8"
    )
    scopes_dir = project_dir / "config" / "scopes"
    scopes_dir.mkdir(parents=True, exist_ok=True)
    (scopes_dir / "bot.yml").write_text(
        declaration if declaration is not None else _MINIMAL_DECL, encoding="utf-8"
    )


def _service(home: Path, app_config: AppConfig) -> MagicMock:
    service = MagicMock()
    service._project_dir = home
    service.project_dir = home
    service._app_config = app_config
    service._home_persistence = None
    service._mcp_registry = None
    service._bot_model_config = None
    service._default_provider = None
    service._default_pool_name = "main"
    service._pool_session_store = MagicMock(spec=PoolRoutingStore)
    service._model_choice_registry = None
    service._transcript_store = None
    service._output_adapter_factory = None
    service._on_subagent_created = None
    service.control_channel = MagicMock()
    service.command_processor = MagicMock()
    service.workspace_stack = None
    service._strategy_registry = None
    service._component_registry = ComponentRegistry()
    service._component_registry.register(
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        "file_prompt",
        FilePromptProviderFactory(),
    )
    return service


def _pool_instance() -> MagicMock:
    instance = MagicMock()
    instance.root_agent_name = "main"
    instance.pool._agents = {}
    instance.pool.shutdown_all = AsyncMock(return_value=True)
    instance.broker_bridge.start = AsyncMock()
    instance.terminal_manager = None
    instance.mcp_manager = None
    return instance


async def _build_home_resources(
    service: MagicMock,
) -> Any:
    """Materialize the HOME workspace of ``service`` via the real
    ``_build_resources`` path (create_pool recorded, everything else real)."""
    home = service._project_dir
    ctx = WorkspaceContext.from_target(home, data_dir_name=".modex", home=home)
    recorded: dict[str, Any] = {}

    async def _create_pool(**kwargs: Any) -> MagicMock:
        recorded.update(kwargs)
        return _pool_instance()

    with (
        patch("bot.service.pool.create_pool", side_effect=_create_pool),
        patch("bot.workspace.wiring.resources.BackgroundTaskRunner") as background_type,
    ):
        background_type.return_value.start = AsyncMock()
        background_type.return_value.stop = AsyncMock()
        resources = await _build_resources(service, ctx)
    return resources, recorded


def _landing_manifest(resources: Any, home: Path, data_dir_name: str = ".modex") -> dict[str, Any]:
    """The observable data-landing manifest of one built workspace."""
    root = home / data_dir_name
    return {
        "state_db": (root / "state.db").exists(),
        "skeleton": sorted(p.name for p in root.iterdir() if p.is_dir()),
        "pool_memory": sorted(p.name for p in (root / "memory").iterdir()),
        "session_store": type(resources.session_index_store).__name__,
        "persistence_live": resources.persistence is not None,
    }


# ---------------------------------------------------------------------------
# BASELINE (service-config-driven landing; frozen before the migration lands)
# ---------------------------------------------------------------------------


async def test_baseline_config_driven_sqlite_landing(tmp_path: Path) -> None:
    """The service-config-driven sqlite build: state.db + full skeleton
    under ``<home>/.modex`` — the landing a declaration override must
    reproduce."""
    home = tmp_path / "proj"
    home.mkdir()
    _write_project(home)
    app_config = AppConfig.model_validate(
        {"persistence": {"backend": "sqlite"}, "paths": {"data_dir_name": ".modex"}}
    )
    service = _service(home, app_config)
    manager = WorkspacePersistenceManager(home / ".modex" / "state.db")
    await manager.open()
    service._home_persistence = manager

    resources, _ = await _build_home_resources(service)
    try:
        manifest = _landing_manifest(resources, home)
        assert manifest == {
            "state_db": True,
            "skeleton": [
                "experiences",
                "inbox",
                "memory",
                "overflow",
                "pool_sessions",
                "session_index",
                "sessions",
            ],
            "pool_memory": ["main"],
            "session_store": "SqliteSessionStore",
            "persistence_live": True,
        }
    finally:
        await _stop_resources(resources)
    await manager.close()


async def test_baseline_config_driven_file_landing(tmp_path: Path) -> None:
    """Today's file-backend build: no workspace persistence manager, the
    file-backed session store — the landing a ``file`` declaration must
    reproduce."""
    home = tmp_path / "proj"
    home.mkdir()
    _write_project(home)
    app_config = AppConfig.model_validate(
        {"persistence": {"backend": "file"}, "paths": {"data_dir_name": ".modex"}}
    )
    service = _service(home, app_config)

    resources, _ = await _build_home_resources(service)
    try:
        manifest = _landing_manifest(resources, home)
        assert manifest["session_store"] == "WorkspacePoolSessionStore"
        assert manifest["persistence_live"] is False
        assert manifest["pool_memory"] == ["main"]
        assert (home / ".modex" / "memory" / "main").is_dir()
    finally:
        await _stop_resources(resources)


async def test_baseline_restart_round_trip(tmp_path: Path) -> None:
    """Config-driven restart round-trip: a session record written in round 1
    is visible to the round-2 build over the same data root."""
    home = tmp_path / "proj"
    home.mkdir()
    _write_project(home)
    app_config = AppConfig.model_validate(
        {"persistence": {"backend": "sqlite"}, "paths": {"data_dir_name": ".modex"}}
    )
    service = _service(home, app_config)
    manager = WorkspacePersistenceManager(home / ".modex" / "state.db")
    await manager.open()
    service._home_persistence = manager

    resources, _ = await _build_home_resources(service)
    await resources.persistence.connection.execute(
        "INSERT INTO sessions (session_id, scope_key) VALUES (?, ?)",
        (
            "existing.main",
            '{"session_id":"existing.main","session_prefix":"existing","agent_id":"main"}',
        ),
    )
    await _stop_resources(resources)
    await manager.close()

    manager2 = WorkspacePersistenceManager(home / ".modex" / "state.db")
    await manager2.open()
    service._home_persistence = manager2
    resources2, _ = await _build_home_resources(service)
    try:
        session = await resources2.session_index_store.get("existing.main")
        assert session is not None
        assert session.session_id == "existing.main"
        assert session.agent_name == "main"
    finally:
        await _stop_resources(resources2)
    await manager2.close()


# ---------------------------------------------------------------------------
# MIGRATION (ticket 14): the declaration's workspace layer selects the
# resources; equivalent configs land identically to the frozen baseline.
# ---------------------------------------------------------------------------

_WORKSPACE_DECL = """\
workspace:
  name: decl-test
  persistence:
    backend: sqlite
  paths:
    data_dir_name: .modex
  pools:
    main:
      agents:
        main:
          description: declared main agent
"""

_WORKSPACE_DECL_FILE = """\
workspace:
  name: decl-test
  persistence:
    backend: file
  paths:
    data_dir_name: .modex
  pools:
    main:
      agents:
        main:
          description: declared main agent
"""


async def test_declaration_driven_landing_equals_config_driven(tmp_path: Path) -> None:
    """AC (e): a sqlite/.modex DECLARATION lands exactly where the
    service-config-driven build lands (equivalent selections, identical
    manifest)."""
    config_home = tmp_path / "config-proj"
    config_home.mkdir()
    _write_project(config_home)
    config_app = AppConfig.model_validate(
        {"persistence": {"backend": "sqlite"}, "paths": {"data_dir_name": ".modex"}}
    )
    config_service = _service(config_home, config_app)
    config_manager = WorkspacePersistenceManager(config_home / ".modex" / "state.db")
    await config_manager.open()
    config_service._home_persistence = config_manager
    config_resources, _ = await _build_home_resources(config_service)

    decl_home = tmp_path / "decl-proj"
    decl_home.mkdir()
    _write_project(decl_home, declaration=_WORKSPACE_DECL)
    decl_app = AppConfig.model_validate({})
    decl_service = _service(decl_home, decl_app)
    decl_manager = WorkspacePersistenceManager(decl_home / ".modex" / "state.db")
    await decl_manager.open()
    decl_service._home_persistence = decl_manager
    decl_resources, _ = await _build_home_resources(decl_service)

    try:
        assert _landing_manifest(decl_resources, decl_home) == _landing_manifest(
            config_resources, config_home
        )
    finally:
        await _stop_resources(config_resources)
        await _stop_resources(decl_resources)
    await config_manager.close()
    await decl_manager.close()


async def test_declaration_backend_overrides_service_config(tmp_path: Path) -> None:
    """A declared ``file`` backend overrides the service config's sqlite
    default — the workspace resource selection is the declaration's."""
    home = tmp_path / "proj"
    home.mkdir()
    _write_project(home, declaration=_WORKSPACE_DECL_FILE)
    # Service config says sqlite (the default); the declaration says file.
    app_config = AppConfig.model_validate({"persistence": {"backend": "sqlite"}})
    service = _service(home, app_config)

    resolved = apply_workspace_resource_selection(
        app_config, load_scope_declaration_opt(home / "config" / "scopes" / "bot.yml")
    )
    assert resolved.persistence.backend is PersistenceBackend.FILE
    service._app_config = resolved

    resources, _ = await _build_home_resources(service)
    try:
        manifest = _landing_manifest(resources, home)
        assert manifest["session_store"] == "WorkspacePoolSessionStore"
        assert manifest["persistence_live"] is False
        assert manifest["pool_memory"] == ["main"]
    finally:
        await _stop_resources(resources)


async def test_two_workspaces_different_backends_coexist(tmp_path: Path) -> None:
    """AC (d): a sqlite-declaring workspace and a file-declaring workspace
    coexist in one process with zero leakage — per-(workspace, pool) data
    addressing keeps each workspace's state.db / file tree to itself."""
    sqlite_home = tmp_path / "sqlite-ws"
    sqlite_home.mkdir()
    _write_project(sqlite_home, declaration=_WORKSPACE_DECL)
    file_home = tmp_path / "file-ws"
    file_home.mkdir()
    _write_project(file_home, declaration=_WORKSPACE_DECL_FILE)

    sqlite_service = _service(sqlite_home, AppConfig.model_validate({}))
    sqlite_service._app_config = apply_workspace_resource_selection(
        sqlite_service._app_config,
        load_scope_declaration_opt(sqlite_home / "config" / "scopes" / "bot.yml"),
    )
    sqlite_manager = WorkspacePersistenceManager(sqlite_home / ".modex" / "state.db")
    await sqlite_manager.open()
    sqlite_service._home_persistence = sqlite_manager

    file_service = _service(file_home, AppConfig.model_validate({}))
    file_service._app_config = apply_workspace_resource_selection(
        file_service._app_config,
        load_scope_declaration_opt(file_home / "config" / "scopes" / "bot.yml"),
    )

    sqlite_resources, _ = await _build_home_resources(sqlite_service)
    file_resources, _ = await _build_home_resources(file_service)
    try:
        # The sqlite workspace owns a live persistence manager + state.db;
        # the file workspace owns the file-backed stores.
        assert sqlite_resources.persistence is not None
        assert (sqlite_home / ".modex" / "state.db").exists()
        assert type(sqlite_resources.session_index_store).__name__ == (
            "SqliteSessionStore"
        )
        assert file_resources.persistence is None
        assert type(file_resources.session_index_store).__name__ == (
            "WorkspacePoolSessionStore"
        )

        # Per-(workspace, pool) addressing: each workspace's pool data
        # roots at its own target — no cross-workspace leakage.
        assert (sqlite_home / ".modex" / "memory" / "main").is_dir()
        assert (file_home / ".modex" / "memory" / "main").is_dir()
        assert sqlite_resources.pool_data["main"].memory_dir == (
            sqlite_home / ".modex" / "memory" / "main"
        )
        assert file_resources.pool_data["main"].memory_dir == (
            file_home / ".modex" / "memory" / "main"
        )
    finally:
        await _stop_resources(sqlite_resources)
        await _stop_resources(file_resources)
    await sqlite_manager.close()


async def test_declaration_restart_round_trip(tmp_path: Path) -> None:
    """AC (e): restart round-trip on a workspace declaration — a session
    written in round 1 is visible to the round-2 declaration-driven build."""
    home = tmp_path / "proj"
    home.mkdir()
    _write_project(home, declaration=_WORKSPACE_DECL)
    service = _service(home, AppConfig.model_validate({}))
    service._app_config = apply_workspace_resource_selection(
        service._app_config,
        load_scope_declaration_opt(home / "config" / "scopes" / "bot.yml"),
    )
    manager = WorkspacePersistenceManager(home / ".modex" / "state.db")
    await manager.open()
    service._home_persistence = manager

    resources, _ = await _build_home_resources(service)
    await resources.persistence.connection.execute(
        "INSERT INTO sessions (session_id, scope_key) VALUES (?, ?)",
        (
            "existing.main",
            '{"session_id":"existing.main","session_prefix":"existing","agent_id":"main"}',
        ),
    )
    await _stop_resources(resources)
    await manager.close()

    manager2 = WorkspacePersistenceManager(home / ".modex" / "state.db")
    await manager2.open()
    service._home_persistence = manager2
    resources2, _ = await _build_home_resources(service)
    try:
        session = await resources2.session_index_store.get("existing.main")
        assert session is not None
        assert session.agent_name == "main"
    finally:
        await _stop_resources(resources2)
    await manager2.close()


async def test_pool_as_root_landing_matches_single_workspace_today(tmp_path: Path) -> None:
    """AC (c): a pool-as-root declaration (no workspace layer) behaves like
    today's single-workspace deployment — the domain config drives the
    backend, the landing keeps today's shape, and no workspace selection
    rides the assembly chain."""
    home = tmp_path / "proj"
    home.mkdir()
    _write_project(
        home,
        declaration=(
            "pool:\n"
            "  name: main\n"
            "  agents:\n"
            "    main:\n"
            "      description: pool-as-root main\n"
        ),
    )
    app_config = AppConfig.model_validate(
        {"persistence": {"backend": "sqlite"}, "paths": {"data_dir_name": ".modex"}}
    )
    service = _service(home, app_config)
    manager = WorkspacePersistenceManager(home / ".modex" / "state.db")
    await manager.open()
    service._home_persistence = manager

    spec = load_scope_declaration_opt(home / "config" / "scopes" / "bot.yml")
    assert spec is not None and spec.workspace is None  # zero workspace layer
    service._app_config = apply_workspace_resource_selection(app_config, spec)
    assert service._app_config is app_config  # nothing to override

    resources, recorded = await _build_home_resources(service)
    try:
        # The declared pool boots straight through the declaration road.
        assert recorded.get("declared") is not None
        assert recorded.get("workspace_spec") is None
        assert _landing_manifest(resources, home) == {
            "state_db": True,
            "skeleton": [
                "experiences",
                "inbox",
                "memory",
                "overflow",
                "pool_sessions",
                "session_index",
                "sessions",
            ],
            "pool_memory": ["main"],
            "session_store": "SqliteSessionStore",
            "persistence_live": True,
        }
    finally:
        await _stop_resources(resources)
    await manager.close()
