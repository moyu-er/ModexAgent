"""Experience configuration value object."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ExperienceConfig(BaseModel):
    """Experience review / curator configuration.

    Doubles as the ``experience`` capability's config face (validated at
    compile time from the ``capabilities: {experience: {...}}`` override).
    ``enabled`` is the BIZ pool-deps field (the derived-enablement signal
    threaded to ``build_pool_data``); as a capability knob it is inert —
    enablement is the capability's PRESENCE in the compile product, never
    a config value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    min_messages: int = 10  # minimum messages before review can trigger
    exp_cooldown_turns: int = 3  # turns to wait after exp tool usage (threshold doubled)
    max_iterations: int = 50
    max_experiences: int = 20  # LRU eviction cap — excess are deleted
    curator_interval: int = 86400  # run once per day (seconds)
