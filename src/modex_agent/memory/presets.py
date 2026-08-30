"""Canonical native-agent memory presets — main_agent_memory + subagent_memory.

Single source of truth for native-agent memory presets, owned by the
memory package (SPEC §6.7). Production wiring — ``native_core``'s
subagent default and the BIZ pool wiring — imports the presets from
here. Plain functions only: there is no factory indirection. The module
imports exclusively from ``modex_agent.ioc.configs.memory`` so it
introduces no new package dependency.
"""

from __future__ import annotations

from modex_agent.ioc.configs.memory import (
    ArchiveConfig,
    BudgetConfig,
    CoreMemoryConfig,
    DreamEngineConfig,
    GovernanceConfig,
    MemoryConfig,
    PrunedCatalogConfig,
    SessionConfig,
)


def main_agent_memory(
    *,
    max_context_tokens: int | None = None,
    archive_enabled: bool = False,
    core_enabled: bool = False,
) -> MemoryConfig:
    """Canonical main-agent memory (long-term layers off by default).

    All native main agents get this preset uniformly. ``max_context_tokens``
    is injected from the active model config (``model.yml``) so the session
    compression threshold tracks the model's real context window.

    ``archive_enabled`` / ``core_enabled`` toggle the long-term layers. The
    AND gate (``core_enabled`` requires ``archive_enabled``) is enforced at
    the schema layer by :class:`MemoryToggle`'s validator, NOT here — this
    function performs no validation and merely constructs the requested
    configs. ``dream_engine`` is enabled only when both archive and core are
    on, since the dream engine consolidates archives into core memory.

    Defaults (``archive_enabled=False, core_enabled=False``) are byte-for-byte
    identical to the pre-toggle behavior.
    """
    session = SessionConfig(max_token_ratio=0.85, keep_ratio=0.3)
    if max_context_tokens is not None:
        session = session.model_copy(update={"max_context_tokens": max_context_tokens})
    archive = ArchiveConfig(enabled=True) if archive_enabled else None
    core = CoreMemoryConfig(enabled=True) if core_enabled else None
    dream = DreamEngineConfig(enabled=True) if (archive_enabled and core_enabled) else None
    return MemoryConfig(
        session=session,
        archive=archive,
        core=core,
        dream_engine=dream,
        governance=GovernanceConfig(
            tool_chain_repair=True,
            budget=BudgetConfig(),
        ),
        pruned=PrunedCatalogConfig(enabled=True, max_files=50, topic_max_chars=200),
    )


def subagent_memory() -> MemoryConfig:
    """Canonical subagent memory: session + pruned + governance only.

    No archive/core/dream — subagents are short-lived task workers.
    No experience preset: experience review is main-agent-only.
    """
    return MemoryConfig(
        session=SessionConfig(max_token_ratio=0.85, keep_ratio=0.3),
        archive=None,
        core=None,
        governance=GovernanceConfig(tool_chain_repair=True),
        pruned=PrunedCatalogConfig(enabled=True, max_files=50, topic_max_chars=200),
    )
