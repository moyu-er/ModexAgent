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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from modex_agent.workspace.context import WorkspaceContext

from modex_agent.core.experience import (
    ExperienceManager,
    FileExperienceSource,
    PerFileExperienceMetaStore,
)
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.trace import JsonFileTraceStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolData(PoolDataSnapshot):
    """Frozen bundle of concrete framework objects backing a single pool.

    Inherits the framework contract from :class:`modex_agent.pipeline.snapshot.PoolDataSnapshot`
    and adds ``experience_meta``, which is consumed at wiring time (not during
    the turn itself) when constructing the experience review hook.
    """

    experience_meta: PerFileExperienceMetaStore


def _main_agent_name(pool_cfg: PoolConfig) -> str:
    """Name of the pool's main agent; fallback ``"main"``.

    Re-homed verbatim from ``Workspace._main_agent_name``.
    """
    for agent in pool_cfg.agents:
        if agent.role == "main":
            return agent.name
    return "main"


def _build_experience_manager(
    pool_cfg: PoolConfig,
    experience_dir: Path,
) -> ExperienceManager | None:
    """ExperienceManager only when the main agent enables experience.

    Re-homed verbatim from ``Workspace._build_experience_manager``.
    """
    main_cfg = next(
        (a for a in pool_cfg.agents if a.role == "main"),
        None,
    )
    if main_cfg is None or main_cfg.experience is None:
        return None
    if not main_cfg.experience.enabled:
        return None
    return ExperienceManager(
        source=FileExperienceSource(directories=[experience_dir])
    )


async def build_pool_data(
    ctx: WorkspaceContext,
    pool_name: str,
    pool_cfg: PoolConfig,
    provider: object,
    memory_cfg_factory: Callable[[PoolConfig], MemoryConfig],
    base_system_prompt: str = "",
) -> PoolData:
    """Build one pool's stores + context_manager bound to ``ctx.paths``.

    Re-homed FAITHFULLY from ``Workspace.build_pool_data`` (the first-call
    construction path; caching is the caller's concern here). Binds
    ``self.paths``→``ctx.paths``, ``self._provider``→``provider``,
    ``self._memory_cfg_factory``→``memory_cfg_factory``,
    ``self._pools_config[pool_name]``→``pool_cfg``.
    """
    # Local imports keep the module import graph thin: the codec / store
    # / experience / memory-factory modules are only needed when a pool
    # is actually built, not when this module is imported.
    from modex_agent.agents.react.state import ReActRuntimeStateCodec
    from modex_agent.ioc.factories.memory import create_memory
    from modex_agent.memory.injection import FullInjectionPolicy
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry
    from modex_agent.runtime.enums import AgentKind
    from modex_agent.runtime.store import (
        JsonFileRuntimeCommandStore,
        JsonFileTurnStateStore,
    )

    memory_cfg = memory_cfg_factory(pool_cfg)

    # ── Memory system (memory/<pool>) ────────────────────────────────
    memory_dir = ctx.paths.memory_dir(pool_name)
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_system = create_memory(memory_cfg, provider, memory_dir)  # type: ignore[arg-type]
    await memory_system.initialize()

    # ── Runtime stores (runtime_state/<pool>/{turns,commands,trace}) ─
    codec_registry = RuntimeStateCodecRegistry(
        {AgentKind.REACT: ReActRuntimeStateCodec()}
    )
    turn_store = JsonFileTurnStateStore(
        ctx.paths.runtime_dir(pool_name, "turns"), codec_registry
    )
    command_store = JsonFileRuntimeCommandStore(
        ctx.paths.runtime_dir(pool_name, "commands")
    )
    trace_store = JsonFileTraceStore(
        base_dir=ctx.paths.runtime_dir(pool_name, "trace")
    )

    # ── Experience layer (experiences/<pool>/<main_agent>) ───────────
    main_agent = _main_agent_name(pool_cfg)
    experience_dir = ctx.paths.experience_dir(pool_name, main_agent)
    experience_dir.mkdir(parents=True, exist_ok=True)
    experience_manager = _build_experience_manager(pool_cfg, experience_dir)
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
        command_store=command_store,
        trace_store=trace_store,
        memory_dir=memory_dir,
        runtime_dir=runtime_dir_parent,
        pruned_manager=memory_system.pruned_manager,
        experience_dir=experience_dir,
        experience_meta=experience_meta,
    )
