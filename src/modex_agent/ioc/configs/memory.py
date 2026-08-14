"""Memory system configuration.

MemoryConfig is the most complex config in the system. Each sub-config
has sensible defaults so users only override what they need.

MemoryConfig as an optional field = disabled.
MemoryConfig() = enabled with all defaults.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class ShortTermConfig(BaseModel):
    """Session memory: token-budget triggers for compression."""

    max_context_tokens: int = 200000
    max_token_ratio: float = 0.85
    keep_ratio: float = 0.3
    max_output_tokens: int = 0

    @field_validator("max_token_ratio", mode="after")
    @classmethod
    def _clamp_max_token_ratio(cls, v: float) -> float:
        """Clamp into [0.4, 0.9] per ADR-0009."""
        if v < 0.4:
            return 0.4
        if v > 0.9:
            return 0.9
        return v


class RetentionConfig(BaseModel):
    """Message retention priority during compression."""

    min_recent_user_turns: int = 2
    min_recent_agent_turns: int = 1
    recent_tool_result_count: int = 3


class LongTermConfig(BaseModel):
    """Core memory files (SOUL.md / USER.md / MEMORY.md)."""

    enabled: bool = False
    init_defaults: bool = True
    default_templates_dir: str | None = None


class DreamEngineConfig(BaseModel):
    """Offline archive-to-core-memory consolidation."""

    enabled: bool = False
    interval: int = 1200
    max_consume_per_run: int = 3  # process up to N archives per run


class BudgetConfig(BaseModel):
    """Token-window tool-result pruning configuration.

    Drives ``ContextBudgetGovernance``: when total estimated tokens exceed
    ``governance_ratio × max_context_tokens``, old tool results outside the
    protect window are replaced with a fixed placeholder.

    ``governance_ratio`` must be below ``SessionConfig.max_token_ratio``
    (default 0.85) so governance intervenes *before* persistent compaction.
    """

    governance_ratio: float = 0.60
    protect_tokens: int = 40_000
    min_gain_tokens: int = 20_000
    keep_recent: int = 10
    whitelist_tools: set[str] = Field(default_factory=set)


class SessionConfig(BaseModel):
    """Session memory: token-budget triggers for compression. Replaces ShortTermConfig."""

    max_context_tokens: int = 200000
    max_token_ratio: float = 0.85
    keep_ratio: float = 0.3
    max_output_tokens: int = 0

    @field_validator("max_token_ratio", mode="after")
    @classmethod
    def _clamp_max_token_ratio(cls, v: float) -> float:
        """Clamp into [0.4, 0.9] per ADR-0009."""
        if v < 0.4:
            return 0.4
        if v > 0.9:
            return 0.9
        return v


class ArchiveConfig(BaseModel):
    """Archive memory: compressed history summaries. Separate from KnowledgeConfig."""

    enabled: bool = False
    max_entries: int = 1000
    retained_consumed_pairs: int = 3
    max_archive_count: int = 10  # trigger core memory update when this many undigested
    max_archive_total: int = 20  # max archive dirs on disk (FIFO eviction)
    max_archive_inject: int = 3  # how many recent archives to inject into system prompt
    archive_inject_max_chars: int = 20_000
    archive_inject_step_chars: int = 5_000
    archive_inject_min_chars: int = 5_000
    scope: list[str] = Field(default_factory=lambda: ["user"])  # dimension short-names

    @field_validator("scope", mode="before")
    @classmethod
    def _wrap_scope_str(cls, v: object) -> list[str]:
        """Auto-wrap a single string into a one-element list for backward compat."""
        if isinstance(v, str):
            return [v]
        return v  # type: ignore[return-value]


class CoreMemoryConfig(BaseModel):
    """Core memory: persistent SOUL/USER/MEMORY files. Separate from ArchiveConfig."""

    enabled: bool = False
    default_templates_dir: str | None = None
    scope: list[str] = Field(default_factory=lambda: ["user"])  # dimension short-names

    @field_validator("scope", mode="before")
    @classmethod
    def _wrap_scope_str(cls, v: object) -> list[str]:
        """Auto-wrap a single string into a one-element list for backward compat."""
        if isinstance(v, str):
            return [v]
        return v  # type: ignore[return-value]


class GovernanceConfig(BaseModel):
    """Per-injection context governance pipeline.

    None sub-fields mean that governance stage is disabled.
    """

    tool_chain_repair: bool = True
    budget: BudgetConfig | None = None


class PrunedCatalogConfig(BaseModel):
    """Configuration for pruned memory catalog."""

    enabled: bool = True
    max_files: int = 50
    topic_max_chars: int = 200


class SummarizerAgentConfig(BaseModel):
    """Configuration for the summarizer-as-agent memory system."""

    enabled: bool = True
    context_max_chars: int = 20_000
    core_max_chars: int = 3000
    max_iterations: int = 50


class CompactConfig(BaseModel):
    """Configuration for session-level compact summary generation.

    Compact is always enabled by default — it is the essential session-level
    compression mechanism for all agents (main + subagent).
    """

    enabled: bool = True
    max_output_tokens: int = 8192
    max_iterations: int = 3
    temperature: float = 0.2
    tool_output_max_chars: int = 2000


class MemoryConfig(BaseModel):
    """Memory system configuration.

    None (as an optional field) = memory system not created.
    MemoryConfig() = enabled with all defaults:
      - session layer: on (token-budget compression triggers)
      - compact: on (session-level compact summary)
      - archive/core: off
      - governance/lossy: off
    """

    # New fields
    session: SessionConfig = Field(default_factory=SessionConfig)
    compact: CompactConfig = Field(default_factory=CompactConfig)
    archive: ArchiveConfig | None = Field(default_factory=ArchiveConfig)
    core: CoreMemoryConfig | None = None
    dream_engine: DreamEngineConfig | None = None

    # Summarizer-agent wiring (archive flow)
    summarizer_agent: SummarizerAgentConfig | None = None

    # Existing fields
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    governance: GovernanceConfig | None = None
    pruned: PrunedCatalogConfig | None = None

    # Old fields (backward compat, excluded from serialization)
    short_term: ShortTermConfig | None = Field(default_factory=ShortTermConfig, exclude=True)
    long_term: LongTermConfig | None = Field(default=None, exclude=True)

    def model_post_init(self, __context: Any) -> None:
        """Handle migration from old config format.

        Only migrates when the old-style keys (short_term / long_term) were
        explicitly provided by the caller. This avoids overwriting new-style
        values with defaults.
        """
        # Migrate short_term → session (only if caller explicitly passed short_term).
        # Old short_term carried max_context_tokens (plus now-removed message-count fields);
        # only max_context_tokens survives the token-based redesign.
        if "short_term" in self.model_fields_set and self.short_term is not None:
            logger.warning("MemoryConfig.short_term is deprecated, use session instead")
            object.__setattr__(
                self,
                "session",
                SessionConfig(max_context_tokens=self.short_term.max_context_tokens),
            )

        # Migrate long_term → archive + knowledge (only if caller explicitly passed long_term)
        if "long_term" in self.model_fields_set and self.long_term is not None:
            logger.warning(
                "MemoryConfig.long_term is deprecated, use archive and knowledge instead"
            )
            if self.long_term.enabled:
                if self.archive is None:
                    object.__setattr__(self, "archive", ArchiveConfig(enabled=True))
                else:
                    current = self.archive.model_dump()
                    current["enabled"] = True
                    object.__setattr__(self, "archive", ArchiveConfig(**current))

                if self.core is None:
                    object.__setattr__(
                        self,
                        "core",
                        CoreMemoryConfig(
                            enabled=True,
                            default_templates_dir=self.long_term.default_templates_dir,
                        ),
                    )
                else:
                    current = self.core.model_dump()
                    current["enabled"] = True
                    if self.long_term.default_templates_dir:
                        current["default_templates_dir"] = self.long_term.default_templates_dir
                    object.__setattr__(self, "core", CoreMemoryConfig(**current))
