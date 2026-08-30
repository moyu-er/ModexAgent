"""Pure Terminal-Bench verdict collection from Harbor trial artifacts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from modex_agent.trace.score_injector import ScoreSpec


class VerdictCollectionError(RuntimeError):
    pass


class TrialTrace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trial_id: str
    trace_id: str


class TrialTraceMap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[TrialTrace, ...]


class TerminalBenchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trial_id: str
    value: float


class VerdictProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scorer: Literal["verifier"] = "verifier"
    version: str
    report_source: Literal["official_harness"] = "official_harness"
    run_ref: str


class _TraceArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    trace_id: str


class _VerifierResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    rewards: dict[str, float] | None = None


class _TrialResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    trial_name: str
    verifier_result: _VerifierResult | None = None


type InjectScore = Callable[[str, list[ScoreSpec]], Awaitable[None]]


def read_trial_trace_map(job_dir: Path) -> TrialTraceMap:
    entries: list[TrialTrace] = []
    for path in sorted(job_dir.glob("*/agent/trace-ids.jsonl")):
        records = [
            _TraceArtifact.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not records:
            raise VerdictCollectionError(f"empty trace mapping: {path}")
        entries.append(TrialTrace(trial_id=path.parents[1].name, trace_id=records[-1].trace_id))
    return TrialTraceMap(entries=tuple(entries))


def read_official_results(job_dir: Path) -> tuple[TerminalBenchResult, ...]:
    results: list[TerminalBenchResult] = []
    for path in sorted(job_dir.glob("*/result.json")):
        trial = _TrialResult.model_validate_json(path.read_text(encoding="utf-8"))
        rewards = trial.verifier_result.rewards if trial.verifier_result is not None else None
        if rewards is None or "reward" not in rewards:
            raise VerdictCollectionError(f"official reward missing: {path}")
        results.append(TerminalBenchResult(trial_id=trial.trial_name, value=rewards["reward"]))
    return tuple(results)


def collect_verdict_scores(
    mapping: TrialTraceMap,
    results: tuple[TerminalBenchResult, ...],
    provenance: VerdictProvenance,
) -> list[ScoreSpec]:
    traces = {entry.trial_id: entry.trace_id for entry in mapping.entries}
    comment = provenance.model_dump_json()
    scores: list[ScoreSpec] = []
    for result in results:
        if result.trial_id not in traces:
            raise VerdictCollectionError(f"trace mapping missing for trial: {result.trial_id}")
        scores.append(
            ScoreSpec(
                name="verdict_terminalbench",
                value=result.value,
                data_type="NUMERIC",
                comment=comment,
            )
        )
    return scores


async def inject_verdict_scores(
    mapping: TrialTraceMap,
    results: tuple[TerminalBenchResult, ...],
    provenance: VerdictProvenance,
    inject: InjectScore,
) -> None:
    traces = {entry.trial_id: entry.trace_id for entry in mapping.entries}
    scores = collect_verdict_scores(mapping, results, provenance)
    for result, score in zip(results, scores, strict=True):
        await inject(traces[result.trial_id], [score])


__all__ = [
    "InjectScore",
    "TerminalBenchResult",
    "TrialTrace",
    "TrialTraceMap",
    "VerdictCollectionError",
    "VerdictProvenance",
    "collect_verdict_scores",
    "inject_verdict_scores",
    "read_official_results",
    "read_trial_trace_map",
]
