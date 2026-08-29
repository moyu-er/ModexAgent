"""Workspace-scoped store resolution helpers for the WebUI.

Extracted from :class:`bot.service.web_ui_service.WebUIService` so the
workspace store resolution logic is reusable independently of the
service class. Each function receives the service attributes it reads
as explicit parameters.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.service.session_store import WorkspacePoolSessionStore
from bot.webui.types import RuntimeStores
from modex_agent.core.session_store import SessionStore
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.plugins.defaults.capabilities.todo import TodoSupply

if TYPE_CHECKING:
    from bot.webui.transcript_store import TranscriptStore
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.persistence.managers import WorkspacePersistenceManager


def workspace_persistence_for_data_root(
    home_resources: Any,
    workspace_stack: Any,
    data_root: Path,
) -> WorkspacePersistenceManager | None:
    """Return the live persistence owner for one workspace data root."""
    resources_by_workspace: list[Any] = []
    if home_resources is not None:
        resources_by_workspace.append(home_resources)
    if workspace_stack is not None:
        resources_by_workspace.extend(workspace_stack.registry.iter_materialized_resources())
    for resources in resources_by_workspace:
        if resources.ctx.paths.root == data_root.resolve():
            return resources.persistence
    return None


async def materialize_workspace(workspace_stack: Any, ws_root: Path) -> Any:
    """Get-or-open + materialize a workspace, returning its resources.

    Shared helper for all WebUI endpoints that need to resolve
    per-workspace stores (session store, transcript store, runtime
    stores). The registry caches materialized resources, so repeated
    calls for the same workspace are cheap.
    """
    workspace_context = await workspace_stack.registry.get_or_open(ws_root)
    return await workspace_stack.registry.materialize(workspace_context)


async def session_store_for_index(
    app_config: AppConfig,
    workspace_stack: Any,
    index_dir: Path,
    *,
    data_dir_name: str | None = None,
    pool_resolver: Callable[[SessionInfo], str] | None = None,
) -> SessionStore:
    """Resolve the session index store for a workspace.

    FILE backend: builds a :class:`WorkspacePoolSessionStore` from the
    index dir (requires ``data_dir_name`` + ``pool_resolver``).
    SQLITE backend: materializes the workspace and returns its
    ``session_index_store``.
    """
    if app_config.persistence.backend is PersistenceBackend.FILE:
        assert data_dir_name is not None
        assert pool_resolver is not None
        return WorkspacePoolSessionStore(
            base_dir=index_dir,
            pool_resolver=pool_resolver,
            data_dir_name=data_dir_name,
        )
    resources = await materialize_workspace(workspace_stack, index_dir.parent.parent)
    return resources.session_index_store


async def workspace_transcript_store_for_sessions(
    workspace_stack: Any,
    sessions_dir: Path,
) -> TranscriptStore:
    """Materialize a workspace and return its configured transcript adapter."""
    resources = await materialize_workspace(workspace_stack, sessions_dir.parent.parent)
    transcript_store = resources.workspace_transcript_store
    if transcript_store is None:
        raise RuntimeError(
            f"Database transcript persistence is unavailable for {sessions_dir.parent.parent}"
        )
    return transcript_store


async def resolve_runtime_stores(
    workspace_stack: Any,
    app_config: AppConfig | None,
    ws_root: Path,
    pool: str,
) -> RuntimeStores:
    """Resolve backend-aware runtime stores for the WebUI endpoints.

    Returns a :class:`RuntimeStores` from the materialized workspace
    resources when in SQLite mode, or an empty ``RuntimeStores()`` in
    FILE mode (endpoints fall back to their hardcoded file-based stores).

    The todo panel reads the pool's todo capability supply — the SAME
    store instance the todo tools write through (identity parity; the
    capability is the single construction authority). A pool whose agents
    do not carry the ``todo`` capability has no supply entry: the panel
    falls back to the FILE-mode store (an empty read — no agent ever
    writes todos there; the dark-supply death, SPEC P5).
    """
    if app_config is None or app_config.persistence.backend is PersistenceBackend.FILE:
        return RuntimeStores()
    # Materialize the workspace on demand (same pattern as
    # session_store_for_index) so the resolver works even before the
    # first agent turn materializes the workspace.
    resources = await materialize_workspace(workspace_stack, ws_root)
    # turn_store comes from PoolDataSnapshot (per-pool).
    pool_data = resources.pool_data.get(pool)
    turn_store = pool_data.turn_store if pool_data is not None else None
    # todo_store comes from the pool's capability supply (the pool's
    # materialize deps carry the same mapping Stage 3 aggregated).
    todo_store = None
    instance = resources.pools.get(pool)
    deps = instance.pool.materialize_deps if instance is not None else None
    if deps is not None:
        supply = deps.capability_supply.get("todo")
        if isinstance(supply, TodoSupply):
            todo_store = supply.store
    return RuntimeStores(todo_store=todo_store, turn_store=turn_store)
