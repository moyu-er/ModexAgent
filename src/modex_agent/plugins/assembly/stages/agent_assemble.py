"""Stage 4: construct the authoritative native agent runtime."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from modex_agent.plugins.assembly.native_core import (
    NativeAssemblyInputs,
    assemble_native_agent,
)
from modex_agent.plugins.assembly.pipeline import AssemblyStage

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.builder import AssemblyBuilder
    from modex_agent.plugins.assembly.context import AssemblyContext
    from modex_agent.plugins.assembly.spec import AssemblySpec


NativeInputsFactory = Callable[
    ["AssemblySpec", "AssemblyBuilder", "AssemblyContext"],
    NativeAssemblyInputs,
]


class AgentAssembleStage(AssemblyStage):
    """Delegate native construction to the shared assembly core."""

    def __init__(self, inputs_factory: NativeInputsFactory) -> None:
        self._inputs_factory = inputs_factory

    async def process(
        self,
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        ctx: AssemblyContext,
    ) -> None:
        inputs = self._inputs_factory(spec, builder, ctx)
        result = await assemble_native_agent(
            spec,
            ctx.registry,
            inputs,
            ctx=ctx,
        )
        builder.agent = result.instance
        builder.descriptor = result.descriptor
        builder.mcp_manager = result.mcp_backend
