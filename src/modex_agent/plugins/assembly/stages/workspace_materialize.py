"""Stage 1 — WorkspaceMaterializeStage.

Lazily builds the per-workspace resource bundle ``R`` via
``ScopeRegistry.materialize`` (cache + LRU + single-flight). The
registry owns the build/evict lifecycle; the stage only triggers
materialization and forwards the result to ``builder.workspace_resources``.

Supplied-mode (CRITICAL — prevents recursive single-flight deadlock):

    ``ctx.workspace_resources`` is pre-filled (non-None) when the assembly
    pipeline runs INSIDE a ``ResourceFactory.materialize`` body that already
    contains a pool loop calling ``create_pool`` (see BIZ evidence:
    ``examples/bot_project/bot/workspace/wiring/resources.py:83-107,261-302``).
    Without this skip, the call chain

        factory.materialize(ctx)
          -> _assemble_resources
            -> for pool in pools: create_pool(...)
              -> pipeline.run(spec, ctx)
                -> WorkspaceMaterializeStage.process
                  -> ctx.workspace_registry.materialize(ctx.workspace_ctx)
                    -> _materialize_once  # same target key
                      -> factory.materialize(ctx)  # RE-ENTRY

    would re-enter ``factory.materialize`` for the SAME target key. The
    registry's single-flight guard (``_inflight``) stores the in-flight
    task — the very task currently running — so awaiting it deadlocks.

    The orchestrator breaks the cycle by pre-filling
    ``ctx.workspace_resources`` with the in-construction ``R`` before
    calling ``pipeline.run`` inside the factory body. The stage copies the
    supplied value to ``builder.workspace_resources`` (so downstream stages
    like PoolAssembleStage can read it) and skips the registry call.

The stage does NOT register a cleanup callback — ``ScopeRegistry``
owns eviction (``evict_and_release`` / ``evict_all``); the assembly
pipeline's cleanup contract concerns resources it created, not resources
borrowed from the registry's cache.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.plugins.assembly.pipeline import AssemblyStage

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.builder import AssemblyBuilder
    from modex_agent.plugins.assembly.context import AssemblyContext
    from modex_agent.plugins.assembly.spec import AssemblySpec


class WorkspaceMaterializeStage(AssemblyStage):
    """Assembly stage 1 — materialize the per-workspace resource bundle ``R``.

    See module docstring for the supplied-mode no-op invariant and the
    recursive single-flight deadlock it prevents.
    """

    async def process(
        self,
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        ctx: AssemblyContext,
    ) -> None:
        if ctx.workspace_resources is not None:
            builder.workspace_resources = ctx.workspace_resources
            return
        resources = await ctx.workspace_registry.materialize(ctx.workspace_ctx)
        builder.workspace_resources = resources
