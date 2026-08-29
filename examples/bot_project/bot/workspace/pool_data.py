"""Re-homed PoolData + build_pool_data (REHOME; purely additive).

These units re-home — FAITHFULLY, logic verbatim — the SOUND construction logic
that lived on the old ``Workspace`` class
(:mod:`bot.workspace.pool_data`). They are standalone, testable units
that bind paths to a passed :class:`WorkspaceContext` instead of ``self.paths``.

The experience layer (manager + dir + meta store) died with the
experience capability's supply face: ``ExperienceCapability.supply``
builds them from the compile product (SPEC §8.3) — this module no longer
constructs any experience resource.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.scope import BotRecordScope
from modex_agent.core.provider import LLMProvider
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.scope.spec import AgentSpec
from modex_agent.workspace.context import WorkspaceContext

if TYPE_CHECKING:
    from modex_agent.trace.otel_store import OtelSpanTraceStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolData(PoolDataSnapshot):
    """Frozen bundle of concrete framework objects backing a single pool.

    Inherits the framework contract from :class:`modex_agent.pipeline.snapshot.PoolDataSnapshot`.
    The retired per-pool experience fields (dir + meta store) died with
    the experience capability's supply face — the capability supply owns
    them now.
    """


async def build_pool_data(
    ctx: WorkspaceContext,
    pool_name: str,
    root_agent: AgentSpec,
    provider: LLMProvider | None,
    assembly_deps: PoolAssemblyDeps,
    base_system_prompt: str = "",
    *,
    app_config: AppConfig | None = None,
    persistence: WorkspacePersistenceManager | None = None,
    trace_store: OtelSpanTraceStore | None = None,
) -> PoolData:
    """Build one pool's stores; ``trace_store`` is a caller-owned injection seam
    (``None`` on the production path — the tracing capability's supply builds
    the store; the harbor trial passes its collector-backed ``PoolTraceStore``)."""
    # Local imports keep the module import graph thin: the codec / store
    # / memory-factory modules are only needed when a pool
    # is actually built, not when this module is imported.
    from bot.service.builders import build_memory_registry, build_turn_state_store
    from modex_agent.agents.react.state import ReActRuntimeStateCodec
    from modex_agent.ioc.factories.memory import create_memory
    from modex_agent.memory.injection import FullInjectionPolicy
    from modex_agent.memory.injection.archive import ArchiveInjectionConfig
    from modex_agent.persistence.config import PersistenceBackend
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry
    from modex_agent.runtime.enums import AgentKind

    memory_cfg = assembly_deps.memory
    if memory_cfg is None:
        raise ValueError("PoolAssemblyDeps.memory must be non-None when calling build_pool_data")

    # ── Memory system (memory/<pool>) ────────────────────────────────
    from bot.memory.token_estimator import TiktokenTokenEstimator

    memory_dir = ctx.paths.memory_dir(pool_name)
    memory_dir.mkdir(parents=True, exist_ok=True)
    registry = build_memory_registry(
        app_config,
        persistence,
        memory_dir,
        BotRecordScope(pool=pool_name, workspace_id=str(ctx.target)),
    )
    memory_system = create_memory(
        memory_cfg,
        provider,
        memory_dir,
        token_estimator=TiktokenTokenEstimator(),
        store_registry=registry,
    )
    await memory_system.initialize()

    # ── Runtime stores (runtime_state/<pool>/{turns,trace}) ──
    codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
    turn_store = build_turn_state_store(
        app_config,
        persistence,
        ctx.paths.runtime_dir(pool_name, "turns"),
        codec_registry,
    )
    decision_coordinator = None
    if (
        app_config is not None
        and persistence is not None
        and app_config.persistence.backend is PersistenceBackend.SQLITE
    ):
        from modex_agent.persistence.coordinator import SqliteDecisionCoordinator

        decision_coordinator = SqliteDecisionCoordinator(
            persistence.connection,
            codec_registry,
        )
    # The trace store is NO LONGER built here: the `tracing` capability's
    # supply owns construction (TracingCapability.supply — the BIZ block
    # died with the capability convergence, ADR-0047). The caller-carried
    # ``trace_store`` injection seam (the harbor trial's collector-backed
    # store) stays: it rides PoolDataSnapshot.trace_store into the supply
    # view, which ADOPTS it instead of building. Per-turn consumers
    # (turn_context_builder / TrainingDataHook) read the store the
    # workspace wiring stamps onto the snapshot from the capability
    # supply after assembly — the same instance, one owner.

    # ── Context manager ──────────────────────────────────────────────
    # The experience injection rides the capability-section channel now
    # (ExperienceCapability.assemble → the experience.injection section;
    # SPEC §8.3) — no experience construction here.
    runtime_dir_parent = ctx.paths.runtime_dir(pool_name, "turns").parent
    context_manager = MemorySystemContextManager(
        memory_system=memory_system,
        default_agent_id=root_agent.name,
        default_agent_role="main",
        base_system_prompt=base_system_prompt,
        injection_policy=FullInjectionPolicy(),
        archive_injection_config=ArchiveInjectionConfig(
            count=memory_cfg.archive.max_archive_inject,
            max_chars=memory_cfg.archive.archive_inject_max_chars,
            step_chars=memory_cfg.archive.archive_inject_step_chars,
            min_chars=memory_cfg.archive.archive_inject_min_chars,
        )
        if memory_cfg.archive is not None and memory_cfg.archive.enabled
        else ArchiveInjectionConfig(count=0),
        roles=list(root_agent.roles),
    )

    return PoolData(
        context_manager=context_manager,
        turn_store=turn_store,
        trace_store=trace_store,
        memory_dir=memory_dir,
        runtime_dir=runtime_dir_parent,
        pruned_manager=memory_system.pruned_manager,
        decision_coordinator=decision_coordinator,
    )
