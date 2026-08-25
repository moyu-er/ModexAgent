"""Per-workspace stack assembly + resource closures.

Re-exports the public assembly surface; private symbols must be imported
from their specific submodule (``stack``, ``resources``).
"""

# Stack must initialize before resources, whose service import reads this re-export.
from bot.workspace.wiring.stack import WorkspaceStack, build_workspace_stack  # noqa: I001
from bot.workspace.wiring.resources import build_tool_overflow_interceptor_chain

__all__ = [
    "WorkspaceStack",
    "build_tool_overflow_interceptor_chain",
    "build_workspace_stack",
]
