"""Memory system configuration.

MemoryConfig is the most complex config in the system. Each sub-config
has sensible defaults so users only override what they need.

MemoryConfig as a field in AgentConfig is None = disabled.
MemoryConfig() = enabled with all defaults.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class ShortTermConfig(BaseModel):
    """Session memory: token-budget triggers for compression."""

    max_tokens: int = 200000
    max_token_ratio: float = 0.85
    keep_ratio: float = 0.3

    @field_validator("max_token_ratio", mode="after")
    @classmethod
    def _clamp_max_token_ratio(cls, v: float) -> float:
        """Clamp into [0.4, 0.9] per ADR-0009."""
        if v < 0.4:
            return 0.4
        if v > 0.9:
            return 0.9
        return v


class UserRetentionConfig(BaseModel):
    """User retention buffer config — pruned user context tracking.

    This is NOT something users normally configure. Defaults work
    for nearly all use cases.
    """

    enabled: bool = True
    max_entries: int = 5
    max_user_chars: int = 4000
    max_assistant_chars: int = 4000


class RetentionConfig(BaseModel):
    """Message retention priority during compression."""

    min_recent_user_turns: int = 2
    min_recent_agent_turns: int = 1
    recent_tool_result_count: int = 3


class LongTermConfig(BaseModel):
    """Long-term knowledge files (SOUL.md / USER.md / MEMORY.md)."""

    enabled: bool = False
    init_defaults: bool = True
    default_templates_dir: str | None = None


class DreamEngineConfig(BaseModel):
    """Offline archive-to-knowledge consolidation."""

    enabled: bool = False
    interval: int = 1200
    max_consume_per_run: int = 3  # process up to N archives per run


class LossyConfig(BaseModel):
    """Lossy content compaction for oversized messages.

    Compaction happens in fixed-size steps.  When the conversation length
    exceeds ``n * compact_range_count + buffer``, the oldest
    ``n * compact_range_count`` messages become candidates for compaction.
    """

    tool_result_head_chars: int = 1200
    assistant_head_chars: int = 1200
    agent_head_chars: int = 2000
    user_head_chars: int = 2000
    tool_args_head_chars: int = 2048
    compact_range_count: int = 50


class SessionConfig(BaseModel):
    """Session memory: token-budget triggers for compression. Replaces ShortTermConfig."""

    max_tokens: int = 200000
    max_token_ratio: float = 0.85
    keep_ratio: float = 0.3

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
    max_archive_count: int = 10  # trigger knowledge update when this many undigested
    max_archive_total: int = 20  # max archive dirs on disk (FIFO eviction)
    max_archive_inject: int = 3  # how many recent archives to inject into system prompt
    scope: str = "user"  # "user"→UserScope (per-user isolation), "global"→GlobalScope (shared)


class KnowledgeConfig(BaseModel):
    """Knowledge memory: persistent SOUL/USER/MEMORY files. Separate from ArchiveConfig."""

    enabled: bool = False
    default_templates_dir: str | None = None
    scope: str = "user"  # "user"→UserScope (per-user isolation), "global"→GlobalScope (shared)


class GovernanceConfig(BaseModel):
    """Per-injection context governance pipeline.

    None sub-fields mean that governance stage is disabled.
    """

    tool_chain_repair: bool = True
    lossy_compaction: LossyConfig | None = None


class PrunedCatalogConfig(BaseModel):
    """Configuration for pruned memory catalog."""

    enabled: bool = True
    max_files: int = 50
    topic_max_chars: int = 200


class SummarizerAgentConfig(BaseModel):
    """Configuration for the summarizer-as-agent memory system."""

    enabled: bool = True
    context_max_chars: int = 2000
    knowledge_max_chars: int = 3000
    index_max_chars: int = 200
    max_iterations: int = 50


class MemoryConfig(BaseModel):
    """Memory system configuration.

    None (as a field in AgentConfig) = memory system not created.
    MemoryConfig() = enabled with all defaults:
      - session layer: on (token-budget compression triggers)
      - user retention layer: on (internal, transparent)
      - archive/knowledge: off
      - governance/lossy: off
    """

    # New fields
    session: SessionConfig = Field(default_factory=SessionConfig)
    archive: ArchiveConfig | None = Field(default_factory=ArchiveConfig)
    knowledge: KnowledgeConfig | None = None
    dream_engine: DreamEngineConfig | None = None

    # Summarizer-agent wiring (new agent-based archive flow)
    summarizer_agent: SummarizerAgentConfig | None = None

    # Existing fields
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    user_retention: UserRetentionConfig = Field(default_factory=UserRetentionConfig)
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
        # Old short_term carried max_tokens (plus now-removed message-count fields);
        # only max_tokens survives the token-based redesign.
        if "short_term" in self.model_fields_set and self.short_term is not None:
            logger.warning("MemoryConfig.short_term is deprecated, use session instead")
            object.__setattr__(
                self,
                "session",
                SessionConfig(max_tokens=self.short_term.max_tokens),
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

                if self.knowledge is None:
                    object.__setattr__(
                        self,
                        "knowledge",
                        KnowledgeConfig(
                            enabled=True,
                            default_templates_dir=self.long_term.default_templates_dir,
                        ),
                    )
                else:
                    current = self.knowledge.model_dump()
                    current["enabled"] = True
                    if self.long_term.default_templates_dir:
                        current["default_templates_dir"] = self.long_term.default_templates_dir
                    object.__setattr__(self, "knowledge", KnowledgeConfig(**current))
