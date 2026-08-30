"""NEGATIVE mypy fixture (ticket 04 AC (c)) — NOT collected by pytest.

A factory declaring ``ctx: PoolContext`` reads workspace-layer fields.
This MUST be a mypy error: the declared parameter type is the capability
boundary — a pool-scoped factory cannot reach workspace-layer fields
(path layout, MCP shared handle).
"""

from __future__ import annotations

from pydantic import BaseModel

from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import PoolContext


class _ProbeConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


class PoolFactoryReadingWorkspaceFields(ComponentFactory):
    config_model = _ProbeConfig

    async def create(self, config: BaseModel, ctx: PoolContext) -> object:
        # Both reads are type errors: workspace_ctx and mcp_registry are
        # workspace-layer fields, unreachable from a PoolContext declaration.
        return (ctx.workspace_ctx, ctx.mcp_registry)
