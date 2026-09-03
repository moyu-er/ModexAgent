"""The experience HOOK-slot factory (plan §10.3: moved from defaults/hooks.py).

Config altitude (plan §10.5.1): per-agent review knobs
(``min_messages``, ``exp_cooldown_turns``, ``max_iterations``) are owned
by the agent's hook/reviewer; pool knobs live on the supply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.memory.snapshot import (
    DEFAULT_SNAPSHOT_MAX_CONTENT_LEN,
    DEFAULT_SNAPSHOT_MAX_MESSAGES,
)
from modex_agent.plugins.abc import AgentType, ReactHookFactory
from modex_agent.plugins.defaults.capabilities.experience.capability import (
    require_experience_supply,
)

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import PoolContext
    from modex_agent.plugins.defaults.capabilities.experience.review_hook import (
        ExperienceReviewHook,
    )


class ExperienceReviewHookConfig(BaseModel):
    """Config for ``ExperienceReviewHookFactory`` — snapshot thresholds only.

    The review knobs (``min_messages`` / ``exp_cooldown_turns`` /
    ``max_iterations``) come from the supply's per-agent
    ``review_config_by_agent`` (the capability declaration); the runtime
    deps (review agent, memory system, catalog) are SUPPLIED
    INFRASTRUCTURE read from the context chain. Only snapshot shaping
    lives in hook config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_max_messages: int = DEFAULT_SNAPSHOT_MAX_MESSAGES
    snapshot_max_content_len: int = DEFAULT_SNAPSHOT_MAX_CONTENT_LEN


class ExperienceReviewHookFactory(ReactHookFactory):
    """Factory for ``ExperienceReviewHook`` — background conversation review.

    Main-agent only (``applies_to={native_main}``). The hook submits
    reviews to the pool's ``ExperienceSupply`` (never spawns its own
    tasks); the reviewer builds on the supply's ``review_provider``
    (fail-soft when absent — review skipped with a warning, pool boot
    unaffected, §10.6).
    """

    config_model: ClassVar[type[BaseModel]] = ExperienceReviewHookConfig
    applies_to: ClassVar[set[AgentType] | None] = {AgentType.native_main}

    async def create(  # type: ignore[override]
        self, config: ExperienceReviewHookConfig, ctx: PoolContext
    ) -> ExperienceReviewHook:
        from modex_agent.plugins.defaults.capabilities.experience.review_hook import (
            ExperienceReviewHook,
        )
        from modex_agent.plugins.defaults.capabilities.experience.reviewer import (
            ExperienceReviewAgent,
        )

        agent_name = getattr(ctx, "agent_name", None)
        pool_runtime = ctx.pool_runtime
        if pool_runtime is None:
            raise ValueError(
                "experience_review requires pool_runtime; reference it from "
                "a pool roster assembled through the pipeline"
            )
        if agent_name is None:
            raise ValueError(
                "experience_review requires the assembling agent's identity "
                "(the full-chain AgentContext); reference it from a pool "
                "roster assembled through the pipeline"
            )
        supply = require_experience_supply(pool_runtime)

        # Build + register the reviewer ONLY when a provider exists —
        # missing review LLM is fail-soft (skip reviews with a warning;
        # tool/section/storage/curator stay available; boot unaffected).
        if supply.review_provider is not None and supply.review_agent_for(agent_name) is None:
            supply.register_review_agent(
                agent_name,
                ExperienceReviewAgent(provider=supply.review_provider),
            )

        pool_data = (
            pool_runtime.pool_assembly_ctx.pool_data
            if pool_runtime.pool_assembly_ctx is not None
            else None
        )
        if pool_data is None:
            raise ValueError(
                "experience_review requires the pool's pool_data "
                "(memory system); configure the pool's memory resources"
            )
        memory_system = pool_data.context_manager.memory_system
        if memory_system is None:
            raise ValueError("experience_review requires the pool's memory system")

        review_config = supply.review_config_by_agent.get(agent_name)
        if review_config is None:
            # A review hook on an agent the supply does not know is a
            # broken invariant (the hook name is capability-contributed).
            raise ValueError(
                f"experience_review: the pool's experience supply carries no "
                f"review config for agent {agent_name!r} — the capability "
                "must be effective on this agent"
            )

        return ExperienceReviewHook(
            agent_name=agent_name,
            supply=supply,
            memory_system=memory_system,
            catalog=supply.catalog,
            min_messages=review_config.min_messages,
            exp_cooldown_turns=review_config.exp_cooldown_turns,
            snapshot_max_messages=config.snapshot_max_messages,
            snapshot_max_content_len=config.snapshot_max_content_len,
        )
