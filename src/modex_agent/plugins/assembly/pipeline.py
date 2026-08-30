"""Assembly pipeline runner — async stage orchestration (SPEC §6.1, §6.3).

Defines:

- :class:`AssemblyStage` — ABC for a single assembly stage. Each stage is
  async because :meth:`ExecutionStrategy.assemble_main` is async
  (``multi_agent/execution_strategy.py``) and stages
  ``await strategy.assemble_main(...)``.
- :class:`AssemblyPipeline` — holds the 4 main-agent stage instances
  injected via constructor, runs them in order for main-agent assembly
  types, and on failure runs ``builder.cleanup()`` then re-raises.

Main-agent orchestrator only (SPEC Errata-5): the pipeline runs
``native_main`` and ``external_main`` assemblies. Native main agents run
Stage 4 after strategy assembly; external main agents stop after Stage 3.
Subagents (native and external) are constructed by
``AgentTemplate.materialize`` directly — their per-invocation data
(``parent_session``, ``invocation_id``, materialize deps) does not fit the
per-pool ``AssemblyContext`` factory contract.

Design constraints:

- ABC-based interface (rule 7 — no Protocols).
- Constructor injection — no hardcoded stage classes. Tests inject stubs;
  real stages are wired by ``create_pool`` without changing the pipeline.
- Fully async (SPEC: "管线全异步") — ``run()`` is ``async def``.
- Cleanup-on-failure: ``try/except`` -> ``await builder.cleanup()`` -> ``raise``.
  Exceptions are never swallowed (SPEC §6.1: "装配失败不泄漏资源").

Stage subset table (SPEC §6.3 as revised by Errata-5):

    ============== ======== =================================================
    Agent type     Stages   Subset
    ============== ======== =================================================
    native_main    1->2->3->4 WorkspaceMaterialize, Infra, Pool, Agent
    external_main  1->2->3  WorkspaceMaterialize, Infra, Pool
    native_sub     (none)   AgentTemplate.materialize (direct construction)
    external_sub   (none)   AgentTemplate.materialize -> assemble_sub
    ============== ======== =================================================
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from modex_agent.plugins.abc import AgentType
from modex_agent.plugins.assembly.builder import AssembledAgent, AssemblyBuilder
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.assembly.spec import AssemblySpec


class AssemblyStage(ABC):
    """Abstract base for a single assembly pipeline stage (SPEC §6.1).

    A stage receives:

    - ``spec`` — read-only :class:`AssemblySpec` (the assembly input).
    - ``builder`` — mutable :class:`AssemblyBuilder` (accumulate results here;
      register cleanup callbacks as runtime resources are created).
    - ``ctx`` — :class:`AssemblyContext` (provides ComponentRegistry +
      runtime deps).

    Stages are async because :meth:`ExecutionStrategy.assemble_main` is async
    (``multi_agent/execution_strategy.py``) and stages
    ``await strategy.assemble_main(...)``.
    """

    @abstractmethod
    async def process(
        self,
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        ctx: AssemblyContext,
    ) -> None:
        """Process this stage — modify ``builder`` to accumulate results.

        Args:
            spec: read-only assembly input (agent_type, component names,
                configs). ``spec.agent_type`` drove the stage selection but
                is available to stages for per-type branching.
            builder: mutable accumulator; set fields and register cleanup
                callbacks as runtime resources are created.
            ctx: assembly context (ComponentRegistry + runtime deps).

        Raises:
            Exception: any failure. The pipeline catches it, runs
                ``builder.cleanup()``, and re-raises.
        """
        ...


class AssemblyPipeline:
    """Async main-agent assembly pipeline runner (SPEC §6.1, §6.3, Errata-5).

    Holds the 4 main-agent stage instances injected via constructor (no
    hardcoded classes). Runs them in order for both main-agent
    :class:`AgentType` values. On any stage failure, runs
    ``builder.cleanup()`` (reverse-order teardown) then re-raises the
    original exception.

    The stage instances are injected — the pipeline does NOT import or
    instantiate stage classes. This allows tests to inject stubs and the
    real stages to be wired without changing the pipeline.
    """

    def __init__(
        self,
        workspace_materialize: AssemblyStage,
        infra_assemble: AssemblyStage,
        pool_assemble: AssemblyStage,
        agent_assemble: AssemblyStage,
    ) -> None:
        self._workspace_materialize = workspace_materialize
        self._infra_assemble = infra_assemble
        self._pool_assemble = pool_assemble
        self._agent_assemble = agent_assemble

    def _stages_for(self, agent_type: AgentType) -> list[AssemblyStage]:
        """Return the stage subset for the given agent type (SPEC §6.3).

        Native main agents run four stages; external main agents run three.
        Subagent types raise —
        subagents assemble via ``AgentTemplate.materialize`` (SPEC Errata-5),
        never through this pipeline.
        """
        if agent_type is AgentType.native_main:
            return [
                self._workspace_materialize,
                self._infra_assemble,
                self._pool_assemble,
                self._agent_assemble,
            ]
        if agent_type is AgentType.external_main:
            return [
                self._workspace_materialize,
                self._infra_assemble,
                self._pool_assemble,
            ]
        raise ValueError(
            f"Agent type {agent_type!r} does not assemble via "
            "AssemblyPipeline — subagents construct through "
            "AgentTemplate.materialize (SPEC Errata-5)"
        )

    async def run(self, spec: AssemblySpec, ctx: AssemblyContext) -> AssembledAgent:
        """Run the assembly pipeline for the given main-agent spec.

        Creates an :class:`AssemblyBuilder`, selects the stage subset for
        ``spec.agent_type``, runs stages in order. On any stage failure
        (including :class:`asyncio.CancelledError`), runs
        ``await builder.cleanup()`` (reverse-order teardown) then
        re-raises the original exception. On success, returns
        ``await builder.build_agent()``.

        Args:
            spec: assembly input — ``spec.agent_type`` drives stage selection.
            ctx: assembly context (ComponentRegistry + runtime deps).

        Returns:
            The assembled :class:`AssembledAgent`.

        Raises:
            ValueError: ``spec.agent_type`` is a subagent type (subagents
                assemble via ``AgentTemplate.materialize``).
            BaseException: any stage failure, re-raised after cleanup.
        """
        builder = AssemblyBuilder()
        stages = self._stages_for(spec.agent_type)
        try:
            for stage in stages:
                await stage.process(spec, builder, ctx)
                if builder.propagated_context is not None:
                    ctx = builder.propagated_context
        except BaseException:
            await builder.cleanup()
            raise
        return await builder.build_agent()
