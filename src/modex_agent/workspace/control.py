"""Per-conversation workspace control (/cd /pwd /exit).

Switch mutates ONLY the conversation's current_workspace pointer — it never
deactivates or tears down any workspace (multi-live: many workspaces coexist).
Reuses framework ``CdResult``/``CdError`` and ``parse_user_path``: no parallel
result/error types.
"""
from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from modex_agent.workspace.models import CdError, CdResult
from modex_agent.workspace.parse import parse_user_path
from modex_agent.workspace.port import WorkspaceControlPort
from modex_agent.workspace.registry import WorkspaceRegistry

R = TypeVar("R")


class WorkspaceController[R](WorkspaceControlPort):
    """Drives /cd /pwd /exit for a conversation via registry.

    Implements :class:`framework.workspace.port.WorkspaceControlPort` so the
    framework's per-conversation cd/exit/pwd handlers can drive it without
    depending on this business class.
    """

    def __init__(
        self,
        *,
        registry: WorkspaceRegistry[R],
        data_dir_name: str,
        enabled: bool = True,
    ) -> None:
        self._registry: WorkspaceRegistry[R] = registry
        self._data_dir_name: str = data_dir_name
        self._enabled: bool = enabled

    @property
    def home(self) -> Path:
        return self._registry.home

    async def open_workspace(self, target: str) -> CdResult:
        """Validate + register a workspace WITHOUT mutating any session pointer.

        Used by the WebUI /cd endpoint: switching the tab's default workspace
        must not move any existing conversation. The pointer is stamped per
        conversation at attach time (see server _ws_attach).

        Relative paths are resolved against the home workspace (because there is
        no per-session context for WebUI /cd)."""
        if not self._enabled:
            return CdResult(
                success=False,
                current_path=self.home,
                original_path=self.home,
                notice="workspace switching disabled",
                error=CdError.INVALID_PATH,
            )
        try:
            resolved = parse_user_path(target, base=self.home)
        except ValueError:
            return CdResult(
                success=False,
                current_path=self.home,
                original_path=self.home,
                notice=f"cd: invalid path: '{target}'",
                error=CdError.INVALID_PATH,
            )
        if not resolved.exists():
            return CdResult(
                success=False,
                current_path=self.home,
                original_path=self.home,
                notice=f"cd: path not found: '{resolved}'",
                error=CdError.PATH_NOT_FOUND,
            )
        if not resolved.is_dir():
            return CdResult(
                success=False,
                current_path=self.home,
                original_path=self.home,
                notice=f"cd: not a directory: '{resolved}'",
                error=CdError.NOT_A_DIRECTORY,
            )
        try:
            (resolved / self._data_dir_name).mkdir(parents=True, exist_ok=True)
        except OSError:
            return CdResult(
                success=False,
                current_path=self.home,
                original_path=self.home,
                notice=f"cd: permission denied: '{resolved}'",
                error=CdError.PERMISSION_DENIED,
            )
        await self._registry.get_or_open(resolved)
        return CdResult(
            success=True,
            current_path=resolved,
            original_path=self.home,
            notice=f"cd: workspace ready at {resolved}",
        )
