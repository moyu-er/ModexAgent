"""POSITIVE mypy fixture (ticket 04 BIZ compatibility) — NOT collected.

A business-plugin factory written against the PRE-TICKET pattern —
subclassing bare ``ComponentFactory`` and declaring
``create(config, ctx: AssemblyContext)`` — must keep typechecking
WITHOUT a single line changed after the ABC's ``create`` parameter
moves to the full-chain type (override variance: widening the
parameter to a supertype is legal; ``AgentContext <: AssemblyContext``).
"""

from __future__ import annotations

from pydantic import BaseModel

from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import AssemblyContext


class _ProbeConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


class LegacyBizStyleFactory(ComponentFactory):
    config_model = _ProbeConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> object:  # noqa: ARG002
        pool_runtime = ctx.pool_runtime
        registry = ctx.registry
        return (pool_runtime, registry)
