"""Memory-enabled eval assembly and bounded Dream consolidation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from bot.service.pool.declaration import boot_scope_spec
from modex_agent.core.constants import StopReason
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.core.scope import MemoryAgentRole, MemoryLayerName
from modex_agent.hook import HookSpec, OutcomeFinallyHook
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.memory.consolidation.dream_engine import DreamEngine
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.plugins.assembly.single_agent import (
    SingleAgentAssembled,
    SingleAgentInfra,
    assemble_declared_single_agent,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.trace.langfuse_query import Provenance
from modex_agent.trace.memory_trace_hook import MemoryTraceHook
from modex_agent.trace.root_span_hook import RootSpanHook
from modex_agent.trace.score_injector import L2ScoreInjector, ScoreSpec

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult

logger = logging.getLogger(__name__)

_DEFAULT_DREAM_MAX_CONSUME_PER_RUN: Final = 3
_MEMORY_COUNTER_SCORE_VERSION: Final = "memory_trace.v1"
_BOT_PROJECT: Final = Path(__file__).resolve().parents[2]
_MEMORY_HARNESS_DECLARATION: Final = (
    _BOT_PROJECT / "config" / "scopes" / "eval" / "agents" / "memory-harness.yml"
)


class _MemoryCounterScoreHook(OutcomeFinallyHook):
    def __init__(
        self,
        memory_trace_hook: MemoryTraceHook,
        score_injector: L2ScoreInjector,
    ) -> None:
        self._memory_trace_hook = memory_trace_hook
        self._score_injector = score_injector

    async def on_outcome(self, ctx: AgentContext, result: AgentResult) -> None:
        if result.stop_reason is not StopReason.COMPLETED:
            return
        assert ctx.runtime is not None
        session_id = ctx.session.session_id
        counters = self._memory_trace_hook.read_counters(session_id)
        comment = Provenance(
            scorer="verifier",
            version=_MEMORY_COUNTER_SCORE_VERSION,
            report_source="counters",
            run_ref=session_id,
        ).model_dump_json()
        values = (
            ("memory_cleanup_total", counters.memory_cleanup_total),
            ("memory_consolidation_total", counters.memory_consolidation_total),
            ("memory_context_assembled_total", counters.memory_context_assembled_total),
            ("memory_core_updated_total", counters.memory_core_updated_total),
        )
        scores = [
            ScoreSpec(
                name=name,
                value=float(value),
                data_type="NUMERIC",
                comment=comment,
            )
            for name, value in values
            if value > 0
        ]
        if not scores:
            return
        await self._score_injector.inject_score_batch(
            str(ctx.runtime.state.custom[TurnCustomKey.TRACE_ID]),
            scores,
            observation_id=str(
                ctx.runtime.state.custom[TurnCustomKey.ROOT_SPAN_ID]
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryRuntimeServices:
    """Concrete services required by one memory-enabled eval workspace."""

    assembly: SingleAgentAssembled
    runtime_services: AgentRuntimeServices
    memory_system: DefaultMemorySystem
    context_manager: MemorySystemContextManager
    dream_engine: DreamEngine
    memory_trace_hook: MemoryTraceHook
    memory_config: MemoryConfig


@dataclass(frozen=True, slots=True)
class DreamRunSummary:
    """Bounded Dream result derived from cursor progress, not run booleans."""

    iterations: int
    exhausted: bool
    stalled: bool


async def build_memory_runtime_services(
    workspace: Path,
    provider: LLMProvider,
    base_system_prompt: str = "",
) -> MemoryRuntimeServices:
    """Build a real archive+core eval stack rooted in ``workspace``."""
    from bot.eval.agent_harness import build_runtime_services

    component_registry = ComponentRegistry()
    with PluginRegistrationContext(component_registry) as registration:
        DefaultPlugin().register(registration)
    declaration = load_scope_declaration(_MEMORY_HARNESS_DECLARATION)
    scope_boot = boot_scope_spec(
        declaration,
        project_dir=_BOT_PROJECT,
        data_dir=workspace,
        graphs_dirs=(),
        default_llm_provider="default",
        registry=component_registry,
    )
    assembled = await assemble_declared_single_agent(
        scope_boot.compilation.agents[0],
        SingleAgentInfra(
            llm_provider=provider,
            safety=RuntimeSafetyPolicy(),
            root_provider=None,
            governance_enabled=True,
        ),
        project_dir=_BOT_PROJECT,
        data_dir=workspace,
        component_registry=component_registry,
    )
    memory_system = assembled.context_manager.memory_system
    memory_config = assembled.descriptor.memory_config
    # The declaration owns the default prompt; this argument remains eval input.
    assembled.context_manager.base_system_prompt = base_system_prompt

    runtime_services = build_runtime_services(
        workspace / "trace",
        model=provider.get_default_model(),
    )
    assert assembled.instance.pipeline is not None
    turn_context_builder = assembled.instance.pipeline._turn_context_builder
    assert turn_context_builder is not None
    runtime_services.governance = turn_context_builder.governance
    memory_trace_hook = MemoryTraceHook(runtime_services.trace_store)
    memory_system.add_cleanup_hook(memory_trace_hook)
    assert runtime_services.hooks is not None
    for spec in runtime_services.hooks.hook_specs:
        if isinstance(spec.hook, RootSpanHook) and spec.hook.score_injector is not None:
            runtime_services.hooks.add(
                HookSpec(
                    hook=_MemoryCounterScoreHook(
                        memory_trace_hook,
                        spec.hook.score_injector,
                    )
                )
            )
            break
    dream_engine = _build_dream_engine(
        memory_system,
        max_consume_per_run=(
            memory_config.dream_engine.max_consume_per_run
            if memory_config.dream_engine is not None
            else _DEFAULT_DREAM_MAX_CONSUME_PER_RUN
        ),
    )
    if dream_engine is None:
        raise RuntimeError("Memory eval preset did not create archive and core layers")

    return MemoryRuntimeServices(
        assembly=assembled,
        runtime_services=runtime_services,
        memory_system=memory_system,
        context_manager=assembled.context_manager,
        dream_engine=dream_engine,
        memory_trace_hook=memory_trace_hook,
        memory_config=memory_config,
    )


async def run_dream_until_exhausted(
    memory_system: DefaultMemorySystem,
    *,
    max_iterations: int = 20,
    dream_engine: DreamEngine | None = None,
) -> DreamRunSummary:
    """Run Dream until every archive cursor is exhausted or stops advancing."""
    engine = dream_engine or _build_dream_engine(memory_system)
    if engine is None:
        logger.warning("Dream consolidation failed: archive or core layer is unavailable")
        return DreamRunSummary(iterations=0, exhausted=False, stalled=False)

    records = await memory_system.store_registry.list_records(
        layer=MemoryLayerName.ARCHIVE,
        agent_roles={MemoryAgentRole.MAIN},
    )
    iterations = 0
    for record in records:
        context = record.context
        if context is None:
            continue
        remaining = await memory_system.get_unprocessed_history_count(context)
        while remaining > 0 and iterations < max_iterations:
            iterations += 1
            try:
                await engine.run(context)
            except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
                logger.warning("Dream consolidation failed", exc_info=True)
                return DreamRunSummary(
                    iterations=iterations,
                    exhausted=False,
                    stalled=False,
                )
            next_remaining = await memory_system.get_unprocessed_history_count(context)
            if next_remaining >= remaining:
                logger.warning(
                    "Dream consolidation stalled: session=%s remaining=%d",
                    context.session_id,
                    next_remaining,
                )
                return DreamRunSummary(
                    iterations=iterations,
                    exhausted=False,
                    stalled=True,
                )
            remaining = next_remaining

        if remaining > 0:
            return DreamRunSummary(
                iterations=iterations,
                exhausted=False,
                stalled=False,
            )

    return DreamRunSummary(iterations=iterations, exhausted=True, stalled=False)


def _build_dream_engine(
    memory_system: DefaultMemorySystem,
    *,
    max_consume_per_run: int = _DEFAULT_DREAM_MAX_CONSUME_PER_RUN,
) -> DreamEngine | None:
    archive_manager = memory_system.archive_manager
    core_memory_manager = memory_system.core_memory_manager
    if archive_manager is None or core_memory_manager is None:
        return None
    return DreamEngine(
        history_manager=archive_manager,
        long_term_manager=core_memory_manager,
        registry=memory_system.store_registry,
        consolidator=memory_system.core_memory_consolidator,
        max_consume_per_run=max_consume_per_run,
        hook_runner=memory_system.hook_runner,
    )


__all__ = [
    "DreamRunSummary",
    "MemoryRuntimeServices",
    "build_memory_runtime_services",
    "run_dream_until_exhausted",
]
