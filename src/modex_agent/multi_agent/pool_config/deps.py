"""Pool assembly dependencies value object."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.media import MediaConfig
from modex_agent.memory.config import MemoryConfig


class PoolAssemblyDeps(BaseModel):
    """Frozen runtime-dependency value object.

    Replaces the runtime params previously held by the deleted PoolConfig / AgentConfig.

    The retired ``experience`` field (the root-roster-derived enablement
    flag + curator knobs) died with the experience capability's supply
    face: ``ExperienceCapability.supply`` builds the manager/dir/curator
    from the compile product's capability config (SPEC §8.3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory: MemoryConfig | None = None
    media: MediaConfig = Field(default_factory=MediaConfig)
