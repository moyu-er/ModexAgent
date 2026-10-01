"""FW-bundled MEMORY_SYSTEM factory — the ``default`` framework memory system (W6).

Slot honesty (SPEC §19 Errata-8, W6): the MEMORY_SYSTEM slot previously had
zero producers — the framework default memory system was reachable only
through position-default construction (pool wiring / single-agent assembly
calling :func:`~modex_agent.plugins.assembly.memory_factory.create_memory`
directly). This module registers that SAME construction as the bundled
``default`` factory, so a declaration ``memory_system: default`` compiles
and assembles the framework memory system through the slot path
(``native_core`` resolves ``ComponentSlot.MEMORY_SYSTEM`` per agent).

Parameter ownership (Errata-8 (b), two YAML layers): the factory config is
deliberately EMPTY — every parameter-level input (session window, archive /
core toggles) arrives through the agent's ``memory:`` block, which the
compiler projects onto ``spec.memory_overrides``; the factory applies that
merge over the ``main_agent_memory`` preset, exactly like the
position-default path. The replacement-face semantics are unchanged: a
custom factory still replaces the whole system.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from modex_agent.memory.presets import main_agent_memory
from modex_agent.memory.scope import MemoryAgentRole
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.plugins.assembly.context import AgentContext
from modex_agent.plugins.assembly.memory_factory import create_memory

# ``_merge_memory`` is the single owner of MemoryOverrides application
# (native_core); importing it here keeps the slot factory on the same merge
# semantics as the position-default path instead of re-deriving them.
from modex_agent.plugins.assembly.native_core import _merge_memory
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.scope.components import ComponentFactory

logger = logging.getLogger(__name__)

__all__ = [
    "DefaultContextManagerConfig",
    "DefaultContextManagerFactory",
    "register_default_context_managers",
]


class DefaultContextManagerConfig(BaseModel):
    """Empty config schema — the framework default memory system takes all
    parameters through the agent's ``memory:`` block (spec
    ``memory_overrides``), not a factory config face."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DefaultContextManagerFactory(ComponentFactory):
    """Builds the framework default memory system through the slot path.

    The construction is the position-default one (``main_agent_memory``
    preset + the spec's ``memory:`` overrides → :func:`create_memory` →
    :class:`MemorySystemContextManager`), reading the agent's identity,
    memory overrides, workspace memory dir, and LLM provider from the
    assembly context chain. The system prompt is deliberately NOT baked in
    here: the runtime's descriptor-level prompt (SYSTEM_PROMPT_PROVIDER
    slot + capability sections) owns the prompt; this factory owns the
    memory system.
    """

    config_model = DefaultContextManagerConfig

    async def create(
        self,
        config: BaseModel,
        ctx: AgentContext,
    ) -> MemorySystemContextManager:
        _ = config  # empty config face — parameters ride spec.memory_overrides
        spec = ctx.spec
        if spec is None:
            raise ValueError(
                "the bundled 'default' MEMORY_SYSTEM factory requires the "
                "agent spec on the assembly context chain (agent identity, "
                "pool name, memory overrides)"
            )
        memory_config = _merge_memory(main_agent_memory(), spec.memory_overrides)
        memory_dir = ctx.workspace_ctx.paths.memory_dir(spec.pool_name)
        memory_dir.mkdir(parents=True, exist_ok=True)
        memory_system = create_memory(memory_config, ctx.llm_provider, memory_dir)
        await memory_system.initialize()
        logger.info(
            "Bundled default memory system initialized for %s/%s at %s",
            spec.pool_name,
            spec.agent_name,
            memory_dir,
        )
        return MemorySystemContextManager(
            memory_system=memory_system,
            default_agent_id=spec.agent_name,
            default_agent_role=MemoryAgentRole.MAIN,
            roles=list(spec.roles),
        )


def register_default_context_managers(ctx: PluginRegistrationContext) -> None:
    """Register the bundled ``default`` MEMORY_SYSTEM factory."""
    ctx.register_memory_system("default", DefaultContextManagerFactory())
