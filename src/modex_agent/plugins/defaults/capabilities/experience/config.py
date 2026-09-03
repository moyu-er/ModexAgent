"""Experience capability configuration, split by lifecycle altitude (§10.5.1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ExperiencePoolConfig(BaseModel):
    """Pool-level knobs owned by the ONE ``ExperienceSupply`` per pool.

    Built by the capability's ``supply()`` after arbitration: multiple
    agents declaring DIFFERENT pool-level values fails supply construction
    with a typed error (plan §5.3 — no silent first-pick).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_experiences: int = 20  # LRU eviction cap — excess are deleted
    curator_interval: int = 86400  # run once per day (seconds)


class ExperienceReviewConfig(BaseModel):
    """Per-agent review knobs owned by the enabled agent's review hook/reviewer.

    The supply retains one of these per effective agent
    (``review_config_by_agent``); each agent's hook/reviewer reads its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_messages: int = 10  # minimum messages before review can trigger
    exp_cooldown_turns: int = 3  # turns to wait after exp tool usage (threshold doubled)
    max_iterations: int = 50  # reviewer ReAct iteration budget


class ExperienceCapabilityConfig(BaseModel):
    """The ``capabilities: {experience: {...}}`` declaration face.

    One frozen model validated at compile time; the supply splits it by
    altitude (pool knobs arbitrated pool-wide, review knobs kept
    per-agent). The retired inert ``enabled`` field is deleted —
    capability effectiveness is the only enablement authority.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_messages: int = 10
    exp_cooldown_turns: int = 3
    max_iterations: int = 50
    max_experiences: int = 20
    curator_interval: int = 86400

    def pool_config(self) -> ExperiencePoolConfig:
        return ExperiencePoolConfig(
            max_experiences=self.max_experiences,
            curator_interval=self.curator_interval,
        )

    def review_config(self) -> ExperienceReviewConfig:
        return ExperienceReviewConfig(
            min_messages=self.min_messages,
            exp_cooldown_turns=self.exp_cooldown_turns,
            max_iterations=self.max_iterations,
        )


class ExperienceConfigError(ValueError):
    """Typed boot failure for conflicting pool-level declarations (§10.5.1)."""
