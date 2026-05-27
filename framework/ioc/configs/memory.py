"""Memory system configuration.

MemoryConfig is the most complex config in the system. Each sub-config
has sensible defaults so users only override what they need.

MemoryConfig as a field in AgentConfig is None = disabled.
MemoryConfig() = enabled with all defaults.
"""

from pydantic import BaseModel, Field


class ShortTermConfig(BaseModel):
    """Session memory: triggers for compression."""

    max_messages: int = 100
    max_tokens: int = 100000
    keep_ratio_for_messages: float = 0.4
    keep_ratio_for_token: float = 0.4


class PendingConfig(BaseModel):
    """Pruned pending input buffer — internal compression mechanism.

    This is NOT something users normally configure. Defaults work
    for nearly all use cases.
    """

    enabled: bool = True
    max_entries: int = 8
    max_chars: int = 12000


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
    min_archive_count: int = 5       # skip consolidation if fewer archives
    max_archive_count: int = 30      # trigger immediately if exceeded
    max_batch_size: int = 20         # process up to N archives per run


class LossyConfig(BaseModel):
    """Lossy content truncation for oversized messages."""

    tool_result_head_chars: int = 1200
    assistant_head_chars: int = 1200
    agent_head_chars: int = 2000
    user_head_chars: int = 4000
    tool_args_head_chars: int = 2048


class GovernanceConfig(BaseModel):
    """Per-injection context governance pipeline.

    None sub-fields mean that governance stage is disabled.
    """

    tool_chain_repair: bool = True
    lossy_compaction: LossyConfig | None = None


class MemoryConfig(BaseModel):
    """Memory system configuration.

    None (as a field in AgentConfig) = memory system not created.
    MemoryConfig() = enabled with all defaults:
      - session layer: on (100 messages / 100k tokens)
      - pending layer: on (internal, transparent)
      - archive/knowledge: off
      - governance/lossy: off
    """

    short_term: ShortTermConfig = Field(default_factory=ShortTermConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    pending: PendingConfig = Field(default_factory=PendingConfig)
    governance: GovernanceConfig | None = None
    long_term: LongTermConfig | None = None
    dream_engine: DreamEngineConfig | None = None
