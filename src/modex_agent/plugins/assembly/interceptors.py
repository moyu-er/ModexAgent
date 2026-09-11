"""Shared declared-interceptor assembly and owned sandbox lifecycle."""

from __future__ import annotations

import asyncio

from modex_agent.core.tool_group import ToolGroupResource
from modex_agent.interceptor.abc import Interceptor
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import AgentContext
from modex_agent.plugins.assembly.resources import AssemblyResourceOwner
from modex_agent.plugins.assembly.spec import AssemblySpec
from modex_agent.sandbox.interceptor import SandboxGuardInterceptor

SANDBOX_GUARD_INTERCEPTOR = "sandbox_guard"


class _SandboxGuardResource(ToolGroupResource):
    def __init__(self, guard: SandboxGuardInterceptor) -> None:
        self._guard = guard
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            await self._guard.close()
            self._closed = True


async def assemble_interceptor_chain(
    spec: AssemblySpec,
    ctx: AgentContext,
    resource_owner: AssemblyResourceOwner,
) -> InterceptorChain | None:
    """Resolve locally declared interceptors and own only local guard runtimes."""
    if not spec.interceptors:
        return None
    pool_runtime = ctx.pool_runtime
    shared = (
        pool_runtime.pool_assembly_ctx.shared_interceptor_chain
        if pool_runtime is not None and pool_runtime.pool_assembly_ctx is not None
        else None
    )
    chain = InterceptorChain(shared.interceptors if shared is not None else [])
    try:
        for name in spec.interceptors:
            factory = ctx.registry.resolve(ComponentSlot.INTERCEPTOR, name)
            config = factory.config_model.model_validate(
                spec.interceptor_configs.get(name, {})
            )
            interceptor = await factory.create(config, ctx)
            if not isinstance(interceptor, Interceptor):
                raise TypeError(
                    f"INTERCEPTOR component {name!r} did not create Interceptor"
                )
            chain.add(interceptor)
            if isinstance(interceptor, SandboxGuardInterceptor):
                resource_owner.adopt(_SandboxGuardResource(interceptor))
    except BaseException as failure:
        await resource_owner.rollback(failure)
        raise
    return chain


__all__ = ["SANDBOX_GUARD_INTERCEPTOR", "assemble_interceptor_chain"]
