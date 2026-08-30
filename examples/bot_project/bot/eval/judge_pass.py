"""Standalone re-judging of existing experiment trajectories."""

from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Final, assert_never

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator

from bot.eval._judge_pass_models import (
    ExperimentWindow,
    JudgePassConfig,
    JudgePassEnvironment,
    JudgePassReport,
    JudgePassResources,
)
from bot.eval.dataset_curator import DatasetCurator
from bot.eval.judge import calibration
from bot.eval.judge._models import JudgeResult, Verdict
from bot.eval.judge.rubrics import load_rubric_set
from bot.eval.judge.runner import JudgeInput, JudgeRunner
from modex_agent.runtime.models import JsonValue
from modex_agent.trace.langfuse_query import _MAX_PAGES, LangfuseClient, LangfuseQueryError
from modex_agent.trace.score_injector import L2ScoreInjector, ScoreSpec

_RUNNER_VERSION: Final = "judge.v1"
_JSON_VALUE_ADAPTER: Final = TypeAdapter(JsonValue)


class _TraceIO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    trace_id: str
    input: str
    output: str

    @field_validator("input", "output", mode="before")
    @classmethod
    def _render_json_value(cls, value: JsonValue) -> str:
        match value:
            case None:
                return ""
            case str() as text:
                return text
            case bool() | int() | float() | list() | dict():
                return _JSON_VALUE_ADAPTER.dump_json(value).decode("utf-8")
            case unreachable:
                assert_never(unreachable)


class JudgePass:
    """Re-judge traces from one immutable experiment window."""

    def __init__(self, resources: JudgePassResources) -> None:
        self._resources = resources

    async def run(
        self,
        config: JudgePassConfig,
        experiment: ExperimentWindow,
    ) -> JudgePassReport:
        """Judge candidates, archive first results, and inject one score batch each."""
        rubric_set = load_rubric_set(config.rubric_set)
        trace_ids = await self._list_trace_ids(experiment, config.limit)
        if not trace_ids:
            self._resources.emit(
                f"no traces for experiment '{experiment.name}'; judged=0"
            )
            return JudgePassReport(
                judged_count=0,
                mean_score=0.0,
                agreement_rate=0.0,
                rubric_version="",
            )

        run_dir = config.archive_root / experiment.name
        run_dir.mkdir(parents=True, exist_ok=True)
        weighted_scores: list[float] = []
        agreeing_repeats = 0
        total_repeats = 0
        rubric_version = ""
        for trace_id in trace_ids:
            raw_io = await self._resources.curator.fetch_trace_io(trace_id)
            if raw_io is None:
                self._resources.emit(
                    f"WARNING trace_id={trace_id} root observation I/O unavailable; skipped"
                )
                continue
            try:
                trace_io = _TraceIO.model_validate(raw_io)
            except ValidationError as error:
                self._resources.emit(
                    f"WARNING trace_id={trace_id} malformed root observation I/O; skipped: {error}"
                )
                continue

            judge_input = JudgeInput(
                item_context=trace_io.input,
                agent_output=trace_io.output,
                rubric_set=rubric_set,
                trace_id=trace_id,
            )
            results = [
                await self._resources.runner.review(judge_input)
                for _ in range(config.repeats)
            ]
            first_result = results[0]
            first_signature = tuple(verdict.verdict for verdict in first_result.verdicts)
            agreeing_repeats += sum(
                tuple(verdict.verdict for verdict in result.verdicts) == first_signature
                for result in results
            )
            total_repeats += len(results)
            rubric_version = first_result.provenance.rubric_version

            (run_dir / f"{trace_id}.json").write_text(
                first_result.model_dump_json(indent=2),
                encoding="utf-8",
            )
            await self._resources.injector.inject_score_batch(
                trace_id,
                self._score_specs(
                    first_result,
                    run_dir,
                    calibrated=calibration.load_calibration_status(
                        config.rubric_set,
                        first_result.provenance.judge_model,
                        calibration.DEFAULT_CALIBRATION_DIR,
                    ).calibrated,
                ),
            )
            weighted_scores.append(first_result.weighted_score)
            self._resources.emit(
                f"trace_id={trace_id} weighted_score={first_result.weighted_score:.6f} "
                f"na_count={first_result.na_count}"
            )

        judged_count = len(weighted_scores)
        mean_score = math.fsum(weighted_scores) / judged_count if judged_count else 0.0
        agreement_rate = agreeing_repeats / total_repeats if total_repeats else 0.0
        self._resources.emit(
            f"judged={judged_count} mean_score={mean_score:.6f} "
            f"agreement={agreement_rate:.1%} repeats={config.repeats}"
        )
        return JudgePassReport(
            judged_count=judged_count,
            mean_score=mean_score,
            agreement_rate=agreement_rate,
            rubric_version=rubric_version,
        )

    async def _list_trace_ids(
        self,
        experiment: ExperimentWindow,
        limit: int | None,
    ) -> list[str]:
        trace_ids: list[str] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            observations, cursor = await self._resources.observation_client.get_observations(
                from_start_time=experiment.start_time,
                to_start_time=experiment.end_time,
                cursor=cursor,
            )
            for observation in observations:
                if observation.parent_observation_id is not None or observation.type != "AGENT":
                    continue
                if observation.trace_id in seen:
                    continue
                seen.add(observation.trace_id)
                trace_ids.append(observation.trace_id)
                if limit is not None and len(trace_ids) >= limit:
                    return trace_ids
            if cursor is None:
                return trace_ids
        raise LangfuseQueryError(
            0,
            f"Observation pagination exceeded the {_MAX_PAGES}-page safety cap",
        )

    @staticmethod
    def _score_specs(
        result: JudgeResult,
        run_dir: Path,
        *,
        calibrated: bool,
    ) -> list[ScoreSpec]:
        comment = calibration.JudgeScoreComment(
            version=f"{_RUNNER_VERSION}+{result.provenance.rubric_version}",
            run_ref=run_dir.as_posix(),
            calibrated=calibrated,
        ).model_dump_json()
        scores = [
            ScoreSpec(
                name="judge_rubric_overall",
                value=result.weighted_score,
                data_type="NUMERIC",
                comment=comment,
            )
        ]
        for verdict in result.verdicts:
            match Verdict(verdict.verdict):
                case Verdict.MET:
                    value = 1.0
                case Verdict.UNMET | Verdict.NA | Verdict.CANNOT_ASSESS:
                    value = 0.0
                case unreachable:
                    assert_never(unreachable)
            scores.append(
                ScoreSpec(
                    name=f"judge_{verdict.criterion}",
                    value=value,
                    data_type="NUMERIC",
                    comment=comment,
                )
            )
        return scores


async def run_judge_pass_from_env(
    config: JudgePassConfig,
    experiment: ExperimentWindow,
    environment: JudgePassEnvironment,
) -> JudgePassReport:
    """Construct and close Langfuse judge-pass resources at the CLI boundary."""
    auth = base64.b64encode(
        f"{environment.public_key}:{environment.secret_key}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {auth}"}
    client = LangfuseClient(
        environment.host,
        environment.public_key,
        environment.secret_key,
    )
    injector = L2ScoreInjector(
        ingestion_url=f"{environment.host.rstrip('/')}/api/public/ingestion",
        headers=headers,
    )
    judge_pass = JudgePass(
        JudgePassResources(
            curator=DatasetCurator(
                langfuse_host=environment.host,
                public_key=environment.public_key,
                secret_key=environment.secret_key,
            ),
            observation_client=client,
            runner=JudgeRunner(environment.provider),
            injector=injector,
            emit=environment.emit,
        )
    )
    try:
        return await judge_pass.run(config, experiment)
    finally:
        await client.close()
        await injector.aclose()


__all__ = [
    "ExperimentWindow",
    "JudgePass",
    "JudgePassConfig",
    "JudgePassEnvironment",
    "JudgePassReport",
    "JudgePassResources",
    "run_judge_pass_from_env",
]
