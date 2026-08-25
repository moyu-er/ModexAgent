"""PoolAssembleStage — pool assembly pipeline stage (SPEC §6.3, stage 3).

Resolves the ``EXECUTION_STRATEGY`` factory from the
:class:`ComponentRegistry`, awaits
:meth:`ExecutionStrategy.assemble_main` on the SUPPLIED
:class:`PoolAssemblyContext`, and records the supplied pool +
:class:`AgentDescriptor` on the builder.

Supply-mode contract (the only mode — SPEC Errata-5): the orchestrator
(``create_pool``) pre-builds the deployment resources and hands them over
via :class:`SupplyInfra`. ``builder.infra.pool_assembly_ctx`` and
``builder.infra.pool`` are REQUIRED; missing supply raises. The earlier
non-supply branches (14-key infra-dict ``_build_pool_assembly_ctx`` +
``_create_agent_pool``) were removed — they were unreachable in
production and their hand-built test shapes masked the real carrier.

Outputs (set on ``builder``):

- ``builder.pool`` — the supplied :class:`AgentPool`.
- ``builder.strategy_result`` — the :class:`StrategyAssembly` returned by
  ``strategy.assemble_main``.
- ``builder.descriptor`` — the :class:`AgentDescriptor` for the main agent
  (consumed by the v2 ``AgentAssembleStage``).

The ``ctx.pool_runtime`` (:class:`PoolRuntimeDeps`) is built and
propagated via ``dataclasses.replace`` — :class:`AssemblyContext` is
frozen (rule 11), so the updated context is passed to
``factory.create(config, ctx)`` rather than mutating in place.
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from modex_agent.commands.handlers import CommandHandler
from modex_agent.commands.processor import SlashCommandProcessor
from modex_agent.interceptor.abc import Interceptor
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import (
    AgentContext,
    PoolRuntimeDeps,
    agent_context_chain,
)
from modex_agent.plugins.assembly.pipeline import AssemblyStage
from modex_agent.tools.terminal import ProcessRegistry

if TYPE_CHECKING:
    from modex_agent.commands.models import CommandProcessor
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        StrategyAssembly,
    )
    from modex_agent.plugins.assembly.builder import AssemblyBuilder
    from modex_agent.plugins.assembly.context import AssemblyContext
    from modex_agent.plugins.assembly.spec import AssemblySpec


class PoolAssembleStage(AssemblyStage):
    """Pool assembly stage — resolves strategy, assembles the main agent.

    SPEC §6.3 stage 3 (runs for both main-agent types). The stage:

    1. Reads the supplied :class:`PoolAssemblyContext` from
       ``builder.infra`` (required — supply-mode is the only mode).
    2. Builds :class:`PoolRuntimeDeps` and propagates it into the context
       via ``dataclasses.replace`` (frozen context — rule 11).
    3. Resolves the ``EXECUTION_STRATEGY`` factory by name from
       ``ctx.registry``.
    4. Creates the :class:`ExecutionStrategy` instance via
       ``factory.create(config, ctx)``.
    5. Awaits ``strategy.assemble_main(pool_assembly_ctx)`` →
       :class:`StrategyAssembly`.
    6. Records the supplied pool + a fresh :class:`AgentDescriptor` on the
       builder. The caller owns the supplied pool's lifecycle — no cleanup
       registration here.
    """

    async def process(
        self,
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        ctx: AssemblyContext,
    ) -> None:
        infra = builder.infra
        if infra is None or infra.pool_assembly_ctx is None or infra.pool is None:
            raise ValueError(
                "PoolAssembleStage requires supply-mode: builder.infra "
                "(SupplyInfra with pool_assembly_ctx + pool) must be "
                "pre-filled by the orchestrator — the self-build branch "
                "was removed (SPEC Errata-5)"
            )

        pool_runtime = PoolRuntimeDeps(pool_assembly_ctx=infra.pool_assembly_ctx)
        ctx = dataclasses.replace(ctx, pool_runtime=pool_runtime)
        builder.propagated_context = ctx

        factory = ctx.registry.resolve(
            ComponentSlot.EXECUTION_STRATEGY, spec.execution_strategy
        )

        config = factory.config_model()
        # Ticket 04: factories receive the full-chain context. The strategy
        # factory is resolved at pool level with the main agent's identity.
        strategy: ExecutionStrategy = await factory.create(
            config, agent_context_chain(ctx, spec=spec)
        )

        strategy_result: StrategyAssembly = await strategy.assemble_main(
            infra.pool_assembly_ctx
        )

        # Invariant: terminal_manager is not None ⇒ process_registry is not
        # None. Strategies that build the terminal trio fill the registry;
        # a third-party strategy supplying only a manager gets a fresh one
        # here — the half-state (manager without registry) is impossible.
        process_registry = strategy_result.process_registry
        if process_registry is None and strategy_result.terminal_manager is not None:
            process_registry = ProcessRegistry()

        pool_runtime = dataclasses.replace(
            pool_runtime,
            session_tree_manager=infra.pool.tree,
            control_channel=strategy_result.control_channel,
            notification_service=(
                infra.notification_service or strategy_result.notification_service
            ),
            binding_store=strategy_result.target_store,
            todo_store=strategy_result.todo_store,
            root_provider=strategy_result.root_provider,
            mcp_registry=infra.pool_assembly_ctx.mcp_registry,
            emitter_factory=infra.pool_assembly_ctx.emitter_factory,
            terminal_manager=strategy_result.terminal_manager,
            process_registry=process_registry,
            communication=infra.communication,
            experience_review_provider=infra.experience_review_provider,
            persistent_bash=strategy_result.persistent_bash,
        )
        # Pool-level extensions (ticket 10) resolve against the
        # pool_runtime-ENRICHED context — the factories may read any
        # pool-layer runtime object (control channel, notification
        # service, ...).
        extension_chain = agent_context_chain(ctx, spec=spec)
        pool_runtime = dataclasses.replace(
            pool_runtime,
            interceptor_chain=await self._resolve_interceptor_chain(
                spec, extension_chain
            ),
            command_processor=await self._resolve_command_processor(
                spec, extension_chain
            ),
        )
        ctx = dataclasses.replace(ctx, pool_runtime=pool_runtime)
        builder.propagated_context = ctx

        builder.pool = infra.pool
        builder.strategy_result = strategy_result
        builder.descriptor = self._create_agent_descriptor(spec)

    # ── private helpers ───────────────────────────────────────────────

    async def _resolve_interceptor_chain(
        self,
        spec: AssemblySpec,
        ctx: AgentContext,
    ) -> InterceptorChain | None:
        """Resolve the spec's INTERCEPTOR roster into a pool-level chain.

        ``None`` when the spec adds no interceptors — the orchestrator
        keeps the workspace-shared chain (ticket 10: this resolution moved
        from the BIZ orchestrator into the pool stage; the factories
        resolve against the pool_runtime-enriched context).
        """
        if not spec.interceptors:
            return None
        infra_shared = ctx.pool_runtime
        shared = (
            infra_shared.pool_assembly_ctx.shared_interceptor_chain
            if infra_shared is not None and infra_shared.pool_assembly_ctx is not None
            else None
        )
        chain = InterceptorChain(shared.interceptors if shared is not None else [])
        for name in spec.interceptors:
            factory = ctx.registry.resolve(ComponentSlot.INTERCEPTOR, name)
            config = factory.config_model.model_validate(
                spec.interceptor_configs.get(name, {})
            )
            interceptor = await factory.create(config, ctx)
            if not isinstance(interceptor, Interceptor):
                raise TypeError(f"INTERCEPTOR component {name!r} did not create Interceptor")
            chain.add(interceptor)
        return chain

    async def _resolve_command_processor(
        self,
        spec: AssemblySpec,
        ctx: AgentContext,
    ) -> CommandProcessor | None:
        """Resolve the spec's COMMAND_HANDLER roster into a processor.

        ``None`` when the spec declares no commands — the orchestrator
        falls back to the passed-in processor or the default (same
        semantics the BIZ resolution had before ticket 10).
        """
        if spec.commands is None:
            return None
        handlers: list[CommandHandler] = []
        for name in spec.commands:
            factory = ctx.registry.resolve(ComponentSlot.COMMAND_HANDLER, name)
            config = factory.config_model.model_validate({})
            handler = await factory.create(config, ctx)
            if not isinstance(handler, CommandHandler):
                raise TypeError(
                    f"COMMAND_HANDLER component {name!r} did not create CommandHandler"
                )
            handlers.append(handler)
        return SlashCommandProcessor(handlers=handlers)

    def _create_agent_descriptor(self, spec: AssemblySpec) -> AgentDescriptor:
        """Construct the main agent's AgentDescriptor from spec identity."""
        return AgentDescriptor(
            address=AgentAddress(name=spec.agent_name),
        )
