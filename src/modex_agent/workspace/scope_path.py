"""ScopePath — the canonical scope address + its single resolver (ticket 15).

SPEC §5.3 addressing convergence: every address into the scope tree is a
:class:`ScopePath` — a workspace root plus an optional pool segment. The
workspace stays the ONLY materialization layer: a pool is not a registry
entry, and a pool's paths resolve within the owning workspace's resource
bundle. :func:`resolve_scope_path` is the single resolution function — it
absorbs the legacy resolver class's two-level ``ws.pool_data.get(pool)``
shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from modex_agent.workspace.resources import WorkspaceManager

if TYPE_CHECKING:
    from modex_agent.pipeline.snapshot import PoolDataSnapshot


class ScopePath(BaseModel):
    """The canonical addressing carrier: ``(workspace_root, pool_name | None)``.

    ``pool_name=None`` addresses the workspace scope itself; a set
    ``pool_name`` addresses one pool of that workspace — the pool's declared
    parent chain ends at the workspace (SPEC §5.3 isolation invariant ④: a
    pool belongs to exactly one workspace).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_root: Path
    pool_name: str | None = None


def resolve_scope_path(
    manager: WorkspaceManager | None, path: ScopePath | None
) -> PoolDataSnapshot | None:
    """Resolve a pool scope path to its data snapshot — the one resolver.

    The workspace segment resolves through ``manager`` (the per-workspace
    resolver seam); the pool segment reads the workspace's resource bundle
    (``ws.pool_data.get(pool_name)``). A workspace-level address (no pool
    segment), an absent manager or path, an unmaterialized workspace, or an
    unknown pool yields ``None`` — callers own their fallbacks. The
    resolution never synthesizes a process-CWD path, which would leak
    across workspaces (ADR-0015 D5).
    """
    if manager is None or path is None or path.pool_name is None:
        return None
    try:
        ws = manager.resolve_workspace()
    except RuntimeError:
        return None
    return ws.pool_data.get(path.pool_name)
