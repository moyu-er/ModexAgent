"""Per-turn effective context budget resolution.

Priority chain (per-model context compaction PRD §4.2, sole definition):

    ModelInfo.context_limit / max_output_tokens  (当轮激活模型, per-turn pipe)
      → fallback  (池级静态配置: MemoryConfig / 装配期默认)

Every consumer (governance, history, assembly) resolves through
``resolve_effective_budget`` — the chain must not be expanded anywhere else.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.capabilities import ModelInfo


class ContextBudget(BaseModel):
    """Effective token budget for one turn, after the priority chain resolves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_context_tokens: int | None = Field(
        default=None,
        description="Effective context-window limit; None = unset/unlimited",
    )
    max_output_tokens: int = Field(
        default=0,
        description="Effective output budget reserved out of the window",
    )


def resolve_effective_budget(
    model_info: ModelInfo | None,
    fallback_max_context_tokens: int | None,
    fallback_max_output_tokens: int = 0,
) -> ContextBudget:
    """Resolve the turn's budget: per-turn ModelInfo first, static fallback second.

    A ``None`` field on ``model_info`` means "not declared for this model" and
    falls back to the pool-level static value; a ``None`` model_info (no
    per-turn override bound) falls back entirely.
    """
    limit = (
        model_info.context_limit
        if model_info is not None and model_info.context_limit is not None
        else fallback_max_context_tokens
    )
    output = (
        model_info.max_output_tokens
        if model_info is not None and model_info.max_output_tokens is not None
        else fallback_max_output_tokens
    )
    return ContextBudget(max_context_tokens=limit, max_output_tokens=output)
