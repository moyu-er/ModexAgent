"""ACI (Agent-Computer Interface) tool enhancements.

Provides the ``aci`` capability's tool — a drop-in replacement for the
standard ``edit`` tool that automatically runs linters after each
successful edit and appends diagnostics to the tool result. Enabled by
declaring ``capabilities: {aci: {}}`` on an agent (the FW-bundled
capability package lives in ``plugins/defaults/capabilities/aci.py``).

The linter subsystem itself lives in :mod:`modex_agent.tools.lint` and
can be used independently of ACI.
"""

from __future__ import annotations

from modex_agent.tools.aci.edit_tool import AciEditTool

__all__ = ["AciEditTool"]
