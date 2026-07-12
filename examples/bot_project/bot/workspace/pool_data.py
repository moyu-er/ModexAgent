"""Re-homed PoolData + build_pool_data (REHOME; purely additive).

These units re-home — FAITHFULLY, logic verbatim — the SOUND construction logic
that lived on the old ``Workspace`` class
(:mod:`bot.workspace.pool_data`). They are standalone, testable units
that bind paths to a passed :class:`WorkspaceContext` instead of ``self.paths``.

The old ``Workspace``/``PoolData`` are untouched here; these re-homed units get
wired in at the CUTOVER task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from modex_agent.core.experience import (
    ExperienceManager,
    FileExperienceSource,
    PerFileExperienceMetaStore,
)
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.experience import ExperienceConfig
from modex_agent.multi_agent.pool_config.specs import PoolSpec
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.trace import JsonFileTraceStore
from modex_agent.workspace.context import WorkspaceContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolData(PoolDataSnapshot):
    """Frozen bundle of concrete framework objects backing a single pool.

    Inherits the framework contract from :class:`modex_agent.pipeline.snapshot.PoolDataSnapshot`
    and adds ``experience_meta``, which is consumed at wiring time (not during
    the turn itself) when constructing the experience review hook.
    """

    experience_meta: PerFileExperienceMetaStore


def _main_agent_name(pool_spec: PoolSpec) -> str:
    """Name of the pool's main agent."""
    return pool_spec.main.agent_name


def _build_experience_manager(
    assembly_deps: PoolAssemblyDeps,
    experience_dir: Path,
) -> ExperienceManager | None:
    """ExperienceManager only when the main agent enables experience."""
    exp_cfg = assembly_deps.experience
    if exp_cfg is None or not exp_cfg.enabled:
        return None
    return ExperienceManager(
        source=FileExperienceSource(directories=[experience_dir])
    )


async def build_pool_data(
    ctx: WorkspaceContext,
    pool_name: str,
    pool_spec: PoolSpec,
    provider: object,
    assembly_deps: PoolAssemblyDeps,
    base_system_prompt: str = "",
) -> PoolData:
    """Build one pool's stores + context_manager bound to ``ctx.paths``."""
    # Local imports keep the module import graph thin: the codec / store
    # / experience / memory-factory modules are only needed when a pool
    # is actually built, not when this module is imported.
    from modex_agent.agents.react.state import ReActRuntimeStateCodec
    from modex_agent.ioc.factories.memory import create_memory
    from modex_agent.memory.injection import FullInjectionPolicy
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry
    from modex_agent.runtime.enums import AgentKind
    from modex_agent.runtime.store import (
        JsonFileTurnStateStore,
    )

    memory_cfg = assembly_deps.memory
    if memory_cfg is None:
        raise ValueError(
            "PoolAssemblyDeps.memory must be non-None when calling build_pool_data"
        )

    # ── Memory system (memory/<pool>) ────────────────────────────────
    from bot.memory.token_estimator import TiktokenTokenEstimator

    memory_dir = ctx.paths.memory_dir(pool_name)
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_system = create_memory(
        memory_cfg, provider, memory_dir, token_estimator=TiktokenTokenEstimator()
    )  # type: ignore[arg-type]
    await memory_system.initialize()

    # ── Runtime stores (runtime_state/<pool>/{turns,trace}) ──
    codec_registry = RuntimeStateCodecRegistry(
        {AgentKind.REACT: ReActRuntimeStateCodec()}
    )
    turn_store = JsonFileTurnStateStore(
        ctx.paths.runtime_dir(pool_name, "turns"), codec_registry
    )
    trace_store = JsonFileTraceStore(
        base_dir=ctx.paths.runtime_dir(pool_name, "trace")
    )

    # ── Experience layer (experiences/<pool>/<main_agent>) ───────────
    main_agent = _main_agent_name(pool_spec)
    experience_dir = ctx.paths.experience_dir(pool_name, main_agent)
    experience_dir.mkdir(parents=True, exist_ok=True)
    experience_manager = _build_experience_manager(assembly_deps, experience_dir)
    experience_meta = PerFileExperienceMetaStore(lambda: experience_dir)

    # ── Context manager ──────────────────────────────────────────────
    runtime_dir_parent = ctx.paths.runtime_dir(pool_name, "turns").parent
    context_manager = MemorySystemContextManager(
        memory_system=memory_system,
        default_agent_id=main_agent,
        default_agent_role="main",
        base_system_prompt=base_system_prompt,
        injection_policy=FullInjectionPolicy(
            pruned_manager=memory_system.pruned_manager
        ),
        experience_manager=experience_manager,
    )

    return PoolData(
        context_manager=context_manager,
        turn_store=turn_store,
        trace_store=trace_store,
        memory_dir=memory_dir,
        runtime_dir=runtime_dir_parent,
        pruned_manager=memory_system.pruned_manager,
        experience_dir=experience_dir,
        experience_meta=experience_meta,
    )
