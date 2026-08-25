"""No-memory control arm execution and utilization classification."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.memory_metric_models import UtilizationClass
from bot.eval.probes._harness_models import ProbeRunRecord, ProbeScore, ScoreFn
from bot.eval.probes.budget import (
    BudgetConfig,
    BudgetedProvider,
    CostCapExceededError,
)
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.types import MessageRole
from modex_agent.trace.pricing import PriceBook


class NomemoryRunConfig(BaseModel):
    """Cost controls for the control-arm calls remaining after the memory arm."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cost_usd: float = Field(gt=0)
    minimum_call_reserve_usd: float = Field(gt=0)
    answer_max_output_tokens: int = Field(ge=1)


class NomemoryRunServices(BaseModel):
    """Collaborators shared with the memory arm without carrying memory state."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    provider: LLMProvider
    pricebook: PriceBook
    score_fn: ScoreFn


class NomemoryProbeResult(BaseModel):
    """One dual-arm control answer and its deterministic score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: ProbeRunRecord
    score: ProbeScore
    cost_usd: float = Field(ge=0)


class NomemoryRunResult(BaseModel):
    """Completed controls plus the bounded partial-run state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    results: list[NomemoryProbeResult]
    spent_cost_usd: float = Field(ge=0)
    cost_capped: bool


def classify_utilization(
    memory_passed: bool,
    nomemory_passed: bool,
) -> UtilizationClass:
    """Map the dual-arm correctness matrix to ticket 11's four classes."""
    return {
        (True, False): UtilizationClass.BENEFICIAL,
        (False, True): UtilizationClass.HARMFUL,
        (False, False): UtilizationClass.IGNORED,
        (True, True): UtilizationClass.NEUTRAL,
    }[(memory_passed, nomemory_passed)]


async def run_nomemory_controls(
    memory_records: list[ProbeRunRecord],
    config: NomemoryRunConfig,
    services: NomemoryRunServices,
) -> NomemoryRunResult:
    """Answer dual-arm probes from only their question, with no memory context."""
    provider = BudgetedProvider(
        services.provider,
        services.pricebook,
        BudgetConfig(
            max_cost_usd=config.max_cost_usd,
            minimum_call_reserve_usd=config.minimum_call_reserve_usd,
        ),
    )
    results: list[NomemoryProbeResult] = []
    cost_capped = False
    for memory_record in memory_records:
        if not memory_record.probe.dual_arm:
            continue
        cost_cursor = provider.spent_cost_usd
        started_at = datetime.now(UTC)
        try:
            response = await provider.chat(
                [ChatMessage(role=MessageRole.USER, content=memory_record.probe.question)],
                temperature=0.0,
                max_output_tokens=config.answer_max_output_tokens,
                tools=None,
            )
        except CostCapExceededError:
            cost_capped = True
            break
        record = ProbeRunRecord(
            probe=memory_record.probe,
            answer=response.content or "",
            assembled_context="",
            trace_id=uuid4().hex,
            span_id=uuid4().hex[:16],
            started_at=started_at,
            completed_at=datetime.now(UTC),
            snapshot_captured_at=memory_record.snapshot_captured_at,
        )
        results.append(
            NomemoryProbeResult(
                record=record,
                score=services.score_fn(record),
                cost_usd=round(provider.spent_cost_usd - cost_cursor, 12),
            )
        )
    return NomemoryRunResult(
        results=results,
        spent_cost_usd=provider.spent_cost_usd,
        cost_capped=cost_capped,
    )


__all__ = [
    "NomemoryProbeResult",
    "NomemoryRunConfig",
    "NomemoryRunResult",
    "NomemoryRunServices",
    "classify_utilization",
    "run_nomemory_controls",
]
