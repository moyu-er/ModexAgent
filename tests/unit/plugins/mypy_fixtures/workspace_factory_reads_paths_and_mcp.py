"""POSITIVE mypy fixture (ticket 04 AC (d)) — NOT collected by pytest.

A factory declaring ``ctx: WorkspaceContext`` reads the path layout
(``workspace_ctx.target``) and the MCP shared handle (``mcp_registry``).
Both live at the workspace layer; this MUST typecheck cleanly. The MCP
handle flows from here into ``connect_mcp(mcp_config, registry=...)``
(ioc/factories/tools.py) as a plain parameter — the chain makes the
handle reachable; connect_mcp stays decoupled from the chain types.
"""

from __future__ import annotations

from pydantic import BaseModel

from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import WorkspaceContext


class _ProbeConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


class WorkspaceFactoryReadingPathsAndMcp(ComponentFactory):
    config_model = _ProbeConfig

    async def create(self, config: BaseModel, ctx: WorkspaceContext) -> object:  # noqa: ARG002
        model_yml = ctx.workspace_ctx.target / "config" / "model.yml"
        return (model_yml, ctx.mcp_registry)
