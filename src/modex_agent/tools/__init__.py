"""Tool system — registry, execution, and management.

MCP integration lives in framework.tools.mcp; import it directly
when MCP support is needed. This facade exports only the stable
tool registry seam.
"""

from __future__ import annotations

from modex_agent.tools.registry import ToolRegistry

__all__ = ["ToolRegistry"]
