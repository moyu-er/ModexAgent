"""ACI (Agent-Computer Interface) tool enhancements.

Provides the ``ToolSupplement.ACI`` tool — a drop-in replacement for the
standard ``edit`` tool that automatically runs linters after each
successful edit and appends diagnostics to the tool result.

The linter subsystem itself lives in :mod:`modex_agent.tools.lint` and
can be used independently of ACI.
"""

from __future__ import annotations

from modex_agent.tools.aci.edit_tool import AciEditTool

__all__ = ["AciEditTool"]
