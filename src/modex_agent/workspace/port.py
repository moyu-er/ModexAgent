"""Framework port for workspace control (cd/exit/pwd slash commands).

The framework's command handlers must NOT depend on any business workspace
controller. They depend on this ABC instead; the business layer
(:class:`framework.workspace.control.WorkspaceController`) implements it.

This keeps rule #5 (framework code must not import business code) intact.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from modex_agent.workspace.models import CdResult


class WorkspaceControlPort(ABC):
    """Framework-facing port for the WebUI workspace API.

    Implementations expose workspace path validation and registration
    (``open_workspace``) and the immutable home path.
    """

    @property
    @abstractmethod
    def home(self) -> Path:
        """The original project directory (immutable startup path)."""

    @abstractmethod
    async def open_workspace(self, target: str) -> CdResult:
        """Validate and register a workspace target path."""
