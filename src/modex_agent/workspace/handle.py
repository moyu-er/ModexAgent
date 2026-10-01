"""Framework face of a per-workspace working-dir handle.

The pool-assembly runtime (execution strategies, the pool factory) reads
exactly one fact off the deployment's workspace handle: the workspace's
current working directory (``.current``). This module owns that minimal
contract plus the two generic :class:`WorkspaceRootProvider` adapters built
on it, so the promoted strategies (W4a) stay business-decoupled. The
concrete handle (fixed per workspace, carrying the business resource
bundle) lives in deployment code and subclasses :class:`WorkspaceHandle`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from modex_agent.core.workspace_root import WorkspaceRootProvider

__all__ = [
    "StaticRootProvider",
    "WorkspaceHandle",
    "WorkspaceHandleRootProvider",
]


class WorkspaceHandle(ABC):
    """Framework contract for a per-workspace working-dir handle.

    Pool assembly reads ``.current`` (the workspace working dir) to anchor
    the sandbox guard's root provider and the external env-spec workspace
    root. Deployments provide the concrete handle; the framework never
    constructs one.
    """

    @property
    @abstractmethod
    def current(self) -> Path:
        """The workspace's current working directory."""
        ...


class WorkspaceHandleRootProvider(WorkspaceRootProvider):
    """WorkspaceRootProvider reading a :class:`WorkspaceHandle`.current.

    One workspace's tools share one handle (and thus one root); a workspace
    switch is a different workspace with its own handle + provider, so the
    provider reads live state without any per-switch wiring.
    """

    def __init__(self, handle: WorkspaceHandle) -> None:
        self._handle: WorkspaceHandle = handle

    def current(self) -> Path:
        return self._handle.current


class StaticRootProvider(WorkspaceRootProvider):
    """WorkspaceRootProvider anchored to one fixed path — the workspace-less
    pool fallback.

    ``create_pool`` is callable without a workspace (hermetic harnesses,
    non-workspace wiring); the sandbox guard factory requires a root on
    every pool boot, so workspace-less pools anchor to the project dir.
    """

    def __init__(self, root: Path) -> None:
        self._root: Path = Path(root).resolve()

    def current(self) -> Path:
        return self._root
