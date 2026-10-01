"""Workspace-root provider contract.

The :class:`WorkspaceRootProvider` ABC sank to core (W3b, formerly defined
in ``tools/workspace_scoped.py``): the sandbox decision service (level 2)
and the workspace-scoped tool wrappers (level 3) both consume the live
workspace root, so the seam lives below both. Implementations stay with
their owners (the tools package and business wiring).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

__all__ = ["WorkspaceRootProvider"]


class WorkspaceRootProvider(ABC):
    """Provides the active workspace working directory (the ``target`` the
    user ``/cd``'d into — the agent's working dir, NOT the ``.modex`` data
    root).

    Implementations must be cheap and read live state, since it is called
    on every tool execution.
    """

    @abstractmethod
    def current(self) -> Path:
        """Return the absolute path of the active workspace working dir."""
        ...
