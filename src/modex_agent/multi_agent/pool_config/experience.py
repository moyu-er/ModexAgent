"""Experience configuration value object."""

from __future__ import annotations

from pydantic import BaseModel


class ExperienceConfig(BaseModel):
    """Experience review / curator configuration."""

    enabled: bool = False
    min_messages: int = 10  # minimum messages before review can trigger
    exp_cooldown_turns: int = 3  # turns to wait after exp tool usage (threshold doubled)
    max_iterations: int = 50
    max_experiences: int = 20  # LRU eviction cap — excess are deleted
    curator_interval: int = 86400  # run once per day (seconds)
