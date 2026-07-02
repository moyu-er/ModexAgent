"""WorkspacePathResolver — workspace-aware path resolution for agent construction.

Two consumers (ADR-0015 D5/D7): the service ack (path strings when
has_output=True) and AgentTemplate.materialize (mkdir output/trace dirs).
Resolution prefers the active workspace's pool_data; ctor args are fallbacks
(used by tests and non-workspace wiring).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.memory.pruned.manager import PrunedManager
    from modex_agent.pipeline.snapshot import PoolDataSnapshot
    from modex_agent.workspace.resources import WorkspaceManager


class WorkspacePathResolver:
    """Resolves per-session paths from the active workspace's pool_data."""

    def __init__(
        self,
        *,
        workspace_manager: "WorkspaceManager | None",
        pool_name: str | None,
        fallback_runtime_dir: Path | None = None,
        fallback_memory_dir: Path | None = None,
        fallback_pruned_manager: "PrunedManager | None" = None,
    ) -> None:
        self._workspace_manager = workspace_manager
        self._pool_name = pool_name
        self._fallback_runtime_dir = fallback_runtime_dir
        self._fallback_memory_dir = fallback_memory_dir
        self._fallback_pruned_manager = fallback_pruned_manager

    def _resolve_pool_data(self) -> "PoolDataSnapshot | None":
        mgr = self._workspace_manager
        if mgr is None or self._pool_name is None:
            return None
        try:
            ws = mgr.resolve_workspace()
        except RuntimeError:
            return None
        if ws is None:
            return None
        return ws.pool_data.get(self._pool_name)

    def runtime_dir(self) -> Path | None:
        pool_data = self._resolve_pool_data()
        if pool_data is not None and pool_data.runtime_dir is not None:
            return pool_data.runtime_dir
        return self._fallback_runtime_dir

    def memory_dir(self) -> Path | None:
        pool_data = self._resolve_pool_data()
        if pool_data is not None and pool_data.memory_dir is not None:
            return pool_data.memory_dir
        return self._fallback_memory_dir

    def pruned_manager(self) -> "PrunedManager | None":
        pool_data = self._resolve_pool_data()
        if pool_data is not None and pool_data.pruned_manager is not None:
            return pool_data.pruned_manager
        return self._fallback_pruned_manager

    def output_path(self, session_id: str) -> Path | None:
        """Per-session OUTPUT.md path under the resolved runtime_dir.

        Returns None when runtime_dir is unresolved (no workspace pool_data
        and no ctor fallback). The caller — AgentTemplate.materialize —
        decides whether to skip the mkdir or raise; this method never
        synthesizes a process-CWD path, which would leak across workspaces.
        """
        runtime = self.runtime_dir()
        if runtime is None:
            return None
        return runtime / "output" / session_id / "OUTPUT.md"

    def trace_dir(self, session_id: str) -> Path | None:
        """Per-session trace dir under the resolved runtime_dir, or None."""
        runtime = self.runtime_dir()
        if runtime is None:
            return None
        return runtime / "trace" / session_id

    @property
    def pool_name(self):
        return self._pool_name
