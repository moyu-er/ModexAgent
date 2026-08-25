"""Manual B1 smoke gate for turn cost injection and read-back."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, assert_never

import anyio
import httpx
import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.eval.live_gates.b1_cost_runtime import (
    REQUIRED_SCORE_NAMES as _REQUIRED_SCORE_NAMES,
)
from bot.eval.live_gates.b1_cost_runtime import (
    GateError,
    PreflightEvidence,
)
from bot.eval.live_gates.b1_cost_runtime import TurnDispatch as TurnDispatch
from bot.eval.live_gates.b1_cost_runtime import dispatch_turn as _dispatch_turn
from bot.eval.live_gates.b1_cost_runtime import read_trace_scores as _read_trace_scores
from bot.eval.live_gates.b1_cost_runtime import run_preflight as _run_preflight
from modex_agent.trace.langfuse_query import (
    LangfuseQueryError,
)

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)

_EVIDENCE_PATH: Final = Path("evals/evidence/b1_cost_smoke.json")


class _CostProvenanceRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scorer: Literal["pricing"]
    version: str
    report_source: Literal["local_pricebook"]
    run_ref: str
    unpriced: list[str]
    price_source: Literal["prices_json", "model_prices_yml"]


class B1CostSmokeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: Literal["b1_cost_smoke"] = "b1_cost_smoke"
    passed: bool
    checked_at: datetime
    preflight: PreflightEvidence
    session_id: str | None = None
    trace_id: str | None = None
    score_count: int = 0
    score_names: list[str] = Field(default_factory=list)
    cost_sum_usd: float = 0.0
    cost_mean_usd: float = 0.0
    unpriced: list[str] = Field(default_factory=list)
    price_source: Literal["prices_json", "model_prices_yml"] | None = None
    error: str | None = None


def _write_evidence(path: Path, evidence: B1CostSmokeEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")


async def run_gate(
    *,
    evidence_path: Path = _EVIDENCE_PATH,
) -> B1CostSmokeEvidence:
    preflight = _run_preflight()
    checked_at = datetime.now(UTC)
    if preflight.missing:
        evidence = B1CostSmokeEvidence(
            passed=False,
            checked_at=checked_at,
            preflight=preflight,
            error="preflight failed: " + ", ".join(preflight.missing),
        )
        _write_evidence(evidence_path, evidence)
        return evidence

    try:
        dispatch = await _dispatch_turn()
        scores = await _read_trace_scores(dispatch.trace_id)
        score_names = sorted(score.name for score in scores)
        if set(score_names) != _REQUIRED_SCORE_NAMES or len(scores) != 13:
            raise GateError(f"expected 13 scores, received {len(scores)}: {score_names}")
        cost_scores = [score for score in scores if score.name == "cost_usd"]
        if len(cost_scores) != 1:
            raise GateError(f"expected one cost_usd score, received {len(cost_scores)}")
        cost_score = cost_scores[0]
        provenance = _CostProvenanceRead.model_validate_json(cost_score.comment or "")
        if provenance.run_ref != dispatch.session_id:
            raise GateError("cost provenance run_ref does not match the turn session")
        match cost_score.value:
            case bool():
                raise GateError("cost_usd must be numeric")
            case int() | float() as numeric_value:
                cost_value = float(numeric_value)
            case unreachable:
                assert_never(unreachable)
        evidence = B1CostSmokeEvidence(
            passed=True,
            checked_at=checked_at,
            preflight=preflight,
            session_id=dispatch.session_id,
            trace_id=dispatch.trace_id,
            score_count=len(scores),
            score_names=score_names,
            cost_sum_usd=cost_value,
            cost_mean_usd=cost_value,
            unpriced=sorted(provenance.unpriced),
            price_source=provenance.price_source,
        )
    except (GateError, LangfuseQueryError, httpx.HTTPError, ValidationError) as exc:
        evidence = B1CostSmokeEvidence(
            passed=False,
            checked_at=checked_at,
            preflight=preflight,
            error=str(exc),
        )
    _write_evidence(evidence_path, evidence)
    return evidence


@app.command()
def main() -> None:
    evidence = anyio.run(run_gate)
    typer.echo(evidence.model_dump_json(indent=2))
    if not evidence.passed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
