"""Converged memory + experience presets with per-pool long-term toggles.

The archive/core toggle is user-editable per pool through ``MemoryToggle`` on
``MainAgentSpec``. Detailed memory configuration (archive/core internals,
governance, pruned, session, and dream-engine construction) and experience
configuration remain baked here.

Converged configuration surface for all native agents:

- **main agent**: ``main_agent_memory()`` (session + pruned + governance
  with lossy compaction; long-term layers off by default) +
  ``main_agent_experience()`` (enabled, ExperienceReviewHook fires)
- **subagent**: ``subagent_memory()`` (session + pruned + governance only —
  no archive/core/dream/experience)

External (external) main agents and subagents are skipped structurally
(template.py early-dispatch + pool_builder external branch + wiring
``pipeline is None`` guard) — these presets never reach them.

``wiring.py`` and ``pool_builder.py`` consume these presets and perform NO
additional memory/experience construction.
"""

from __future__ import annotations

from modex_agent.ioc.configs.memory import (
    ArchiveConfig,
    CoreMemoryConfig,
    DreamEngineConfig,
    GovernanceConfig,
    LossyConfig,
    MemoryConfig,
    PrunedCatalogConfig,
    SessionConfig,
)
from modex_agent.multi_agent.pool_config.experience import ExperienceConfig


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
            lossy_compaction=LossyConfig(
                tool_result_head_chars=1200,
                assistant_head_chars=1200,
                agent_head_chars=2000,
                user_head_chars=4000,
            ),
        ),
        pruned=PrunedCatalogConfig(enabled=True, max_files=50, topic_max_chars=200),
    )


def main_agent_experience() -> ExperienceConfig:
    """Canonical main-agent experience config (review enabled).

    Every native main agent gets ``ExperienceReviewHook`` registered with
    these defaults. The reviewer uses the bot-global default LLM provider
    (``service._default_provider`` from ``model.yml``), NOT per-pool
    provider — so external pools are not special-cased.
    """
    return ExperienceConfig(enabled=True)


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
