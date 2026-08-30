"""Position-derived defaults table (SPEC §3.2).

Root (the derived in-degree-0 node) gets main-agent defaults:
archive/core/experience memory eligibility + approval eligibility + eager
registration + toolset profile ``full``. Non-root gets subagent defaults:
session-only memory + lazy materialization + toolset profile
``read-write``. Every default yields to the node's own declaration —
override precedence is ``framework default < node-local declaration``.

The dead legacy tool-preset field's values land here as position-derived
profiles (SPEC §3.4): root → ``full``, non-root → ``read_write`` —
byte-parity with the legacy main/sub type defaults (frozen split-brain
goldens), so comparisons need no translation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.memory.presets import main_agent_memory, subagent_memory
from modex_agent.scope.spec import AgentSpec
from modex_agent.tools.presets import ToolPreset


class MemoryPreset(StrEnum):
    """Position-derived memory eligibility family (SPEC §3.2 memory row)."""

    ARCHIVE_CORE_EXPERIENCE = "archive_core_experience"
    """Root default — the ``main_agent_memory`` preset family: archive/core
    layers eligible (toggleable), experience review on."""
    SESSION_ONLY = "session_only"
    """Non-root default — the ``subagent_memory`` preset family: session +
    governance + pruned only, no long-term layers, no experience."""


class RegistrationTiming(StrEnum):
    """When a declared agent is registered/materialized (SPEC §3.2)."""

    EAGER = "eager"
    """Root default — registered at boot."""
    LAZY = "lazy"
    """Non-root default — materialized on first dispatch."""


class PositionDefaults(BaseModel):
    """The position-derived defaults table, one row set per position.

    Rows (SPEC §3.2): memory (family + layer states), approval eligibility,
    registration timing, toolset profile. The ``task``-tool row is
    tree-shape derived (has-children), not position derived — it belongs
    to the compiler (ticket 06).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_preset: MemoryPreset
    archive_enabled: bool
    """Archive layer state — off by default; root may toggle via ``memory:``."""
    core_enabled: bool
    """Core layer state — off by default; root may toggle via ``memory:``."""
    approval_eligible: bool
    """Root may enable approval; non-root may not (V9 guards declarations)."""
    registration: RegistrationTiming
    toolset_profile: ToolPreset


def defaults_for_position(*, is_root: bool) -> PositionDefaults:
    """The SPEC §3.2 defaults for a tree position."""
    if is_root:
        return PositionDefaults(
            memory_preset=MemoryPreset.ARCHIVE_CORE_EXPERIENCE,
            archive_enabled=False,
            core_enabled=False,
            approval_eligible=True,
            registration=RegistrationTiming.EAGER,
            toolset_profile=ToolPreset.FULL,
        )
    return PositionDefaults(
        memory_preset=MemoryPreset.SESSION_ONLY,
        archive_enabled=False,
        core_enabled=False,
        approval_eligible=False,
        registration=RegistrationTiming.LAZY,
        toolset_profile=ToolPreset.READ_WRITE,
    )


POSITION_DEFAULT_HOOKS: Final[tuple[str, ...]] = (
    "deliver_retry",
    "length_guard",
    "native_env",
    "loop_detection",
)
"""The SPEC §3.2 hook rows — framework hooks every NATIVE agent's roster
carries by default (both positions; external agents are structurally
excluded — they take no native hook face).

The names enter the compiler's hook merge base exactly like preset tool
names: ``hooks: [-name]`` vetoes one, a declared ``+name`` dedups against
it, and every entry shows a ``position_default`` origin on the bill. The
roster dispatch (``_dispatch_hooks``) resolves them through the HOOK-slot
factories, which derive their per-pool construction deps from the
assembly context chain — the retired code-wired registration function and
the main/sub ``native_env`` constructions died with this table.

Deliberately absent: ``model_choice_bind`` — a bot-project-owned hook
(``BotHooksPlugin``). A framework table naming it would make every
third-party or eval registry (DefaultPlugin-only) reference an
unresolvable component at assembly. It stays declaration-driven: the
shipped ``bot.yml`` declares ``hooks: [+model_choice_bind]`` on its
native mains, and the factory derives its service-scoped deps from the
context chain."""


def position_default_hooks(*, is_root: bool) -> tuple[str, ...]:
    """The position-default hook names for a tree position (SPEC §3.2).

    Both positions carry the same rows today; the ``is_root`` parameter
    keeps the position-table call shape shared with
    :func:`defaults_for_position` so a future position-dependent hook
    lands here without a second table mechanism.
    """
    _ = is_root
    return POSITION_DEFAULT_HOOKS


def effective_defaults(agent: AgentSpec) -> PositionDefaults:
    """Position defaults with the node's own declarations applied.

    Override precedence: framework default < node-local declaration.
    - ``toolset`` overrides the toolset profile.
    - ``eager`` overrides registration timing.
    - ``memory`` archive/core toggles apply within the root-eligible
      family only — a non-root stays session-only (its memory block's
      override face is the session override, not layer toggles).
    - Approval eligibility is positional and not overridable (SPEC §3.2).
    """
    base = defaults_for_position(is_root=agent.is_root)

    archive_enabled = base.archive_enabled
    core_enabled = base.core_enabled
    if agent.memory is not None and base.memory_preset is MemoryPreset.ARCHIVE_CORE_EXPERIENCE:
        archive_enabled = agent.memory.archive_enabled
        core_enabled = agent.memory.core_enabled

    registration = base.registration
    if agent.eager is not None:
        registration = RegistrationTiming.EAGER if agent.eager else RegistrationTiming.LAZY

    toolset_profile = base.toolset_profile
    if agent.toolset is not None:
        toolset_profile = agent.toolset

    return PositionDefaults(
        memory_preset=base.memory_preset,
        archive_enabled=archive_enabled,
        core_enabled=core_enabled,
        approval_eligible=base.approval_eligible,
        registration=registration,
        toolset_profile=toolset_profile,
    )


def memory_config_for_position(
    defaults: PositionDefaults,
    *,
    session_max_context_tokens: int | None = None,
) -> MemoryConfig:
    """The concrete :class:`MemoryConfig` for a position-derived defaults row.

    Ticket 09 (SPEC §3.2 memory row): position — not a caller-side
    main/sub branch — selects the preset family; the resolved
    archive/core toggles and the session threshold (node ``memory:``
    override, else the boot-injected model window) parameterize it.
    """
    if defaults.memory_preset is MemoryPreset.ARCHIVE_CORE_EXPERIENCE:
        return main_agent_memory(
            max_context_tokens=session_max_context_tokens,
            archive_enabled=defaults.archive_enabled,
            core_enabled=defaults.core_enabled,
        )
    cfg = subagent_memory()
    if session_max_context_tokens is None:
        return cfg
    session = cfg.session.model_copy(update={"max_context_tokens": session_max_context_tokens})
    return cfg.model_copy(update={"session": session})
