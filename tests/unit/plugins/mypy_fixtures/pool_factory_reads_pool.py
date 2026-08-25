"""POSITIVE mypy fixture (ticket 04 AC (c)) — NOT collected by pytest.

A factory declaring ``ctx: PoolContext`` reads pool-layer data — the
SPEC §3.3 todo factory shape:
``create(config, ctx: PoolContext) -> TodoWriteTool(ctx.pool_runtime.todo_store)``.
This MUST typecheck cleanly.
"""

from __future__ import annotations

from pydantic import BaseModel

from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import PoolContext


class _ProbeConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


class PoolFactoryReadingPoolLayer(ComponentFactory):
    config_model = _ProbeConfig

    async def create(self, config: BaseModel, ctx: PoolContext) -> object:  # noqa: ARG002
        todo_store = ctx.pool_runtime.todo_store if ctx.pool_runtime is not None else None
        return todo_store
