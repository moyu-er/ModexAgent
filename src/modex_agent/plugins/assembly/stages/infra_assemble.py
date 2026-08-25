"""Stage 2 — InfraAssembleStage (supply-mode contract, SPEC Errata-5).

Consumes the orchestrator-supplied :class:`SupplyInfra` and copies it to
``builder.infra`` verbatim — the per-pool deployment resources
(``pool_assembly_ctx`` + ``pool``) flow from the orchestrator
(``create_pool``) to the later stages unmodified.

INC-4 resolution (final-review convergence, 2026-08-20): this stage used
to also fill ``builder.infra.state_schema_compiler`` from the registry —
a product with ZERO downstream consumers since W6 wired the live
compiler directly by BIZ (``bot/workspace/wiring/resources.py`` passes
``build_state_schema_compiler(...)`` into ``GraphOrchestrator``). The
dead fill and the ``SupplyInfra`` field were deleted:
``build_state_schema_compiler`` now has exactly one production
construction site (the BIZ wiring).

The stage does NOT build broker/inbox/bus/interceptor — those are
per-pool deployment resources the orchestrator (``create_pool`` /
``resources.py``) constructs and carries inside
``SupplyInfra.pool_assembly_ctx``. The earlier self-build branch (FW
InMemoryMessageBroker + trigger resolution) was removed: it was
unreachable in production (supply is always pre-filled since the legacy
dual path was deleted) and its dict shape did not match the production
carrier — the exact "stub masks reality" pattern the scope-converge review
condemned. Special-agent trigger resolution returns with the v2
    per-invocation assembly contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.plugins.assembly.pipeline import AssemblyStage

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.builder import AssemblyBuilder
    from modex_agent.plugins.assembly.context import AssemblyContext
    from modex_agent.plugins.assembly.spec import AssemblySpec


class InfraAssembleStage(AssemblyStage):
    """Assembly stage 2 — copy the orchestrator's supply to ``builder.infra``.

    Supply is REQUIRED: ``ctx.infra`` must be non-None (the orchestrator
    pre-builds the deployment resources; there is no self-build path). A
    missing supply raises — the pipeline is never invoked without an
    orchestrator.
    """

    async def process(
        self,
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        ctx: AssemblyContext,
    ) -> None:
        if ctx.infra is None:
            raise ValueError(
                "InfraAssembleStage requires supply-mode: ctx.infra "
                "(SupplyInfra) must be pre-filled by the orchestrator — "
                "the self-build branch was removed (SPEC Errata-5)"
            )

        builder.infra = ctx.infra
