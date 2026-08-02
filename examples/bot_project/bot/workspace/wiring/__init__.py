"""Per-workspace stack assembly + resource closures.

Re-exports the public assembly surface; private symbols must be imported
from their specific submodule (``stack``, ``resources``, ``pool_wiring``).
"""

from bot.workspace.wiring.stack import (
    WorkspaceStack,
    build_single_workspace_stack,
    build_workspace_stack,
)

__all__ = [
    "WorkspaceStack",
    "build_workspace_stack",
    "build_single_workspace_stack",
]
