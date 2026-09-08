"""Eval harness CLI -- curate datasets, run experiments, compare runs.

Layer 2 of the eval architecture (ADR-0024). Opt-in via the ``[eval]`` extra.
Runs as a separate process to avoid OTel tracer-provider conflicts with the
bot's JSON-OTLP trace path.

Langfuse credentials are read from the environment:

- ``LANGFUSE_HOST`` (default: ``http://localhost:3000``)
- Langfuse public key (required)
- Langfuse secret key (required)

Usage::

    python -m bot.eval.cli curate --dataset react-baseline --max 50
    python -m bot.eval.cli run --dataset react-baseline --experiment v1 --model gpt-4o
    python -m bot.eval.cli compare --dataset react-baseline
    python -m bot.eval.cli setup-judge --name helpfulness \\
        --prompt "Score the helpfulness of the response from 0.0 to 1.0."
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from math import fsum
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal, assert_never
from unittest.mock import patch

import httpx
import typer
from langfuse import Langfuse
from langfuse.api.commons.errors.not_found_error import NotFoundError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.eval.dataset_curator import DatasetCurator
from bot.eval.evalenv import LangfuseCredentials
from bot.eval.evaluators import (
    accuracy_evaluator,
    completion_evaluator,
    response_length_evaluator,
    tool_success_evaluator,
    world_state_evaluator,
)
from bot.eval.experiment_runner import EvalRunner as _BaseEvalRunner
from bot.eval.judge.calibration import JudgeScoreComment
from bot.eval.judge_cli import judge as judge_command
from bot.eval.task_spec import EvalItemSpec, EvalToolset
from modex_agent.core.llm_request import ReasoningEffort
from modex_agent.core.provider import LLMProvider
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.trace.cassette import CassetteRecorder, CassetteReplayEngine
from modex_agent.trace.langfuse_query import (
    _MAX_PAGES,
    LangfuseClient,
    LangfuseQueryError,
    ScoreReadData,
)


def _configure_console_stream(stream: io.TextIOWrapper) -> None:
    if stream.encoding.lower() != "utf-8":
        stream.reconfigure(errors="replace")


if isinstance(sys.stdout, io.TextIOWrapper):
    _configure_console_stream(sys.stdout)
if isinstance(sys.stderr, io.TextIOWrapper):
    _configure_console_stream(sys.stderr)


app = typer.Typer(
    name="bot-eval",
    help="Eval harness -- curate datasets, run experiments, compare runs.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
app.command(
    name="judge",
    help="Re-judge existing experiment traces without re-running them.",
)(judge_command)

_DEFAULT_LANGFUSE_HOST = "http://localhost:3000"
_SCORES_TIMEOUT = httpx.Timeout(10.0)


class CostSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_usd: float
    mean_usd: float
    count: int


class CompareExperimentWindow(BaseModel):
    """Experiment bounds needed for trace-scoped judge score lookup."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    start_time: datetime = Field(alias="startTime")
    end_time: datetime = Field(alias="endTime")
    item_count: int = Field(alias="itemCount", ge=0)


class EvalRunner(_BaseEvalRunner):
    """Apply an optional CLI toolset override before running v2 items."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        system_prompt: str,
        max_iterations: int = 10,
        langfuse_client: Langfuse | None = None,
        mode: Literal["clean", "production"] = "clean",
        cassette: CassetteReplayEngine | None = None,
        recorder: CassetteRecorder | None = None,
        archive_root: Path | None = None,
        toolset: EvalToolset | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(
            provider=provider,
            system_prompt=system_prompt,
            max_iterations=max_iterations,
            langfuse_client=langfuse_client,
            mode=mode,
            cassette=cassette,
            recorder=recorder,
            archive_root=archive_root,
            model=model,
        )
        self._toolset = toolset

    async def task(
        self,
        *,
        item: object,  # noqa: ANN401  # noqa: OBJECT_OK - Langfuse callback boundary
        **kwargs: object,  # noqa: ANN401  # noqa: OBJECT_OK - Langfuse callback boundary
    ) -> dict[str, Any]:
        spec = EvalItemSpec.from_item_input(getattr(item, "input", None))
        if spec is None or self._toolset is None:
            return await super().task(item=item, **kwargs)
        overridden = spec.model_copy(update={"toolset": self._toolset})
        overridden_item = SimpleNamespace(
            id=getattr(item, "id", spec.id),
            input=overridden.model_dump(mode="json"),
        )
        return await super().task(item=overridden_item, **kwargs)


# --- Langfuse credentials ----------------------------------------------------


def _load_langfuse_env() -> tuple[str, str, str]:
    """Read Langfuse credentials from the environment.

    Returns ``(host, public_key, secret_key)``. Exits with code 1 when
    either Langfuse key is missing.
    """
    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        typer.echo(
            "ERROR: Langfuse public and secret key environment variables are required.",
            err=True,
        )
        raise typer.Exit(code=1)
    host = credentials.host if credentials.host is not None else _DEFAULT_LANGFUSE_HOST
    return host, credentials.public_key, credentials.secret_key


def _basic_auth_header(public_key: str, secret_key: str) -> dict[str, str]:
    """Build a Basic auth header from Langfuse keys."""
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --- curate ------------------------------------------------------------------


@app.command(name="curate", help="Curate an eval dataset from production Langfuse traces.")
def curate(
    dataset: Annotated[str, typer.Option("--dataset", help="Target dataset name.")],
    max_items: Annotated[int, typer.Option("--max", help="Maximum items to curate.")] = 50,
    filter_errors: Annotated[
        bool,
        typer.Option(
            "--filter-errors/--no-filter-errors",
            help="Select error traces (interesting cases).",
        ),
    ] = True,
    filter_high_latency: Annotated[
        bool,
        typer.Option(
            "--filter-high-latency/--no-filter-high-latency",
            help="Select high-latency traces (interesting cases).",
        ),
    ] = False,
    latency_threshold: Annotated[
        float,
        typer.Option(
            "--latency-threshold",
            help="Latency threshold in milliseconds for --filter-high-latency.",
        ),
    ] = 10000,
) -> None:
    """Curate an eval dataset from production Langfuse traces."""
    host, public_key, secret_key = _load_langfuse_env()
    curator = DatasetCurator(
        langfuse_host=host,
        public_key=public_key,
        secret_key=secret_key,
    )
    count = asyncio.run(
        curator.curate(
            dataset_name=dataset,
            max_items=max_items,
            filter_errors=filter_errors,
            filter_high_latency=filter_high_latency,
            latency_threshold_ms=latency_threshold,
        )
    )
    typer.echo(f"Curated {count} items into dataset '{dataset}'")


# --- run ---------------------------------------------------------------------


@app.command(name="run", help="Run an experiment against a Langfuse dataset.")
def run(
    dataset: Annotated[str, typer.Option("--dataset", help="Dataset to run against.")],
    experiment: Annotated[str, typer.Option("--experiment", help="Experiment run name.")],
    model: Annotated[
        str,
        typer.Option("--model", help="Model name, e.g. gpt-4o."),
    ],
    system_prompt: Annotated[
        str,
        typer.Option("--system-prompt", help="System prompt for the agent."),
    ] = "You are a helpful assistant.",
    max_iterations: Annotated[
        int,
        typer.Option("--max-iterations", help="ReAct loop cap per item."),
    ] = 10,
    max_concurrency: Annotated[
        int,
        typer.Option("--max-concurrency", help="Max concurrent item executions."),
    ] = 5,
    toolset: Annotated[
        EvalToolset | None,
        typer.Option("--toolset", help="Override each v2 item's toolset."),
    ] = None,
    mode: Annotated[
        Literal["clean", "production"],
        typer.Option("--mode", help="Agent harness mode."),
    ] = "clean",
) -> None:
    """Run an experiment against a Langfuse dataset.

    Credentials use ``TEST_LLM_API_KEY`` and ``TEST_LLM_BASE_URL`` when set;
    otherwise the direct-HTTP provider falls back to ``OPENAI_API_KEY``
    (openai-compatible routing).
    """
    host, public_key, secret_key = _load_langfuse_env()
    provider = create_llm_provider(
        LLMConfig(
            model=model,
            api_key=os.environ.get("TEST_LLM_API_KEY") or "",
            base_url=os.environ.get("TEST_LLM_BASE_URL") or "",
        )
    )
    langfuse_client = Langfuse(
        base_url=host,
        public_key=public_key,
        secret_key=secret_key,
    )
    runner = EvalRunner(
        provider=provider,
        system_prompt=system_prompt,
        max_iterations=max_iterations,
        langfuse_client=langfuse_client,
        mode=mode,
        archive_root=Path("evals/runs"),
        toolset=toolset,
        model=model,
    )
    result = runner.run(
        dataset_name=dataset,
        experiment_name=experiment,
        evaluators=[
            accuracy_evaluator,
            completion_evaluator,
            response_length_evaluator,
            world_state_evaluator,
            tool_success_evaluator,
        ],
        max_concurrency=max_concurrency,
    )
    typer.echo(result.format())


# --- compare -----------------------------------------------------------------


@app.command(name="compare", help="Compare experiment runs for a dataset.")
def compare(
    dataset: Annotated[str, typer.Option("--dataset", help="Dataset to compare runs for.")],
) -> None:
    """List experiment runs for a dataset with their aggregated scores.

    Uses the Langfuse v4 ``experiments`` API (v3 dataset-runs endpoint is
    disabled in events_only mode). Run-time scores use the experiment window;
    post-hoc judge scores are read from the experiment's root traces.
    """
    host, public_key, secret_key = _load_langfuse_env()
    headers = _basic_auth_header(public_key, secret_key)

    # Resolve dataset ID from name (experiments API returns datasetId, not name).
    lf = Langfuse(base_url=host, public_key=public_key, secret_key=secret_key)
    try:
        ds = lf.get_dataset(dataset)
    except NotFoundError:
        typer.echo(f"Dataset '{dataset}' not found.")
        return
    dataset_id = ds.id

    experiments = _fetch_experiments(
        host=host,
        headers=headers,
        dataset_id=dataset_id,
    )
    if not experiments:
        typer.echo(f"No experiment runs found for dataset '{dataset}'.")
        typer.echo("(Tip: experiments are created by 'run' — check local archives in evals/runs/)")
        return

    costs = asyncio.run(
        _fetch_experiment_costs(
            host,
            (public_key, secret_key),
            experiments,
        )
    )
    judge_scores = asyncio.run(
        _fetch_experiment_judge_scores(
            host,
            (public_key, secret_key),
            experiments,
        )
    )

    typer.echo(f"Experiment runs for dataset '{dataset}':")
    typer.echo()
    typer.echo(f"{'Run':<45} {'Items':<6} {'Cost':<35} {'Scores'}")
    typer.echo(f"{'---':<45} {'---':<6} {'---':<35} {'---'}")

    for exp, cost, posthoc_judge_scores in zip(
        experiments,
        costs,
        judge_scores,
        strict=True,
    ):
        scores = _fetch_experiment_scores(
            host=host,
            headers=headers,
            start_time=exp["startTime"],
            end_time=exp["endTime"],
            judge_scores=posthoc_judge_scores,
        )
        if cost is None:
            cost_text = "(unavailable)"
        elif cost.count == 0:
            cost_text = "(no cost)"
        else:
            cost_text = f"sum=${cost.total_usd:.6f} mean=${cost.mean_usd:.6f}"
        typer.echo(
            f"{exp['name'][:45]:<45} {exp.get('itemCount', '?'):<6} {cost_text:<35} {scores}"
        )


def _fetch_experiments(
    *,
    host: str,
    headers: dict[str, str],
    dataset_id: str | None,
) -> list[dict[str, Any]]:
    """Fetch experiments for a dataset via the v4 ``experiments`` API."""
    url = f"{host.rstrip('/')}/api/public/experiments"
    params: dict[str, Any] = {
        "fromStartTime": "2000-01-01T00:00:00Z",
        "toStartTime": "2100-01-01T00:00:00Z",
        "limit": 100,
    }
    try:
        with httpx.Client(timeout=_SCORES_TIMEOUT) as client:
            response = client.get(url, params=params, headers=headers)
    except Exception:
        return []

    if response.status_code != 200:
        return []

    try:
        body = response.json()
    except Exception:
        return []

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []

    experiments = [exp for exp in data if isinstance(exp, dict)]
    if dataset_id is None:
        return experiments
    return [exp for exp in experiments if exp.get("datasetId") == dataset_id]


def _fetch_experiment_scores(
    *,
    host: str,
    headers: dict[str, str],
    start_time: str,
    end_time: str,
    judge_scores: list[ScoreReadData] | None = None,
) -> str:
    """Fetch and aggregate scores for an experiment via ``v3/scores``.

    Filters by the experiment's ``startTime``–``endTime`` window (the v4
    ``v3/scores`` endpoint does not support ``experimentId`` filtering).
    A 2-second buffer is added to ``end_time`` because score timestamps can
    land exactly on the experiment boundary and be excluded by an open
    upper range. Returns a compact summary like ``accuracy=75%,
    completion=80%`` or ``"(no scores)"`` / ``"(unavailable)"`` on failure.
    """
    url = f"{host.rstrip('/')}/api/public/v3/scores"
    params: dict[str, str | int] = {
        "fields": "core,details,subject",
        "fromTimestamp": start_time,
        "toTimestamp": _buffer_end_time(end_time),
        "limit": 100,
    }
    try:
        with httpx.Client(timeout=_SCORES_TIMEOUT) as client:
            response = client.get(url, params=params, headers=headers)
    except Exception:
        return "(unavailable)"

    if response.status_code != 200:
        return "(unavailable)"

    try:
        body = response.json()
    except Exception:
        return "(unavailable)"

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return "(no scores)"

    judge_score_keys = {
        (score.name, score.subject.id if score.subject is not None else None)
        for score in judge_scores or []
        if score.name.startswith("judge_")
    }
    score_rows: list[tuple[str, float | bool, str | None]] = []
    for score in data:
        if not isinstance(score, dict):
            continue
        name = score.get("name")
        value = score.get("value")
        if not isinstance(name, str) or not isinstance(value, int | float):
            continue
        subject = score.get("subject")
        subject_id = subject.get("id") if isinstance(subject, dict) else None
        if name.startswith("judge_") and (name, subject_id) in judge_score_keys:
            continue
        comment = score.get("comment")
        score_rows.append((name, value, comment if isinstance(comment, str) else None))
    if judge_scores is not None:
        score_rows.extend(
            (score.name, score.value, score.comment)
            for score in judge_scores
            if score.name.startswith("judge_")
        )

    name_values: dict[str, list[float]] = defaultdict(list)
    uncalibrated_judge_names: set[str] = set()
    for name, value, comment in score_rows:
        name_values[name].append(float(value))
        if name.startswith("judge_") and comment is not None:
            try:
                parsed_comment = JudgeScoreComment.model_validate_json(comment)
            except ValidationError:
                continue
            if not parsed_comment.calibrated:
                uncalibrated_judge_names.add(name)

    if not name_values:
        return "(no scores)"

    parts: list[str] = []
    for name, values in sorted(name_values.items()):
        avg = sum(values) / len(values)
        rendered = f"{name}={avg:.0%}" if avg <= 1.0 else f"{name}={avg:.1f}"
        marker = "*" if name in uncalibrated_judge_names else ""
        parts.append(f"{rendered}{marker}")
    return ", ".join(parts)


async def _fetch_posthoc_judge_scores(
    client: LangfuseClient,
    experiment: CompareExperimentWindow,
) -> list[ScoreReadData]:
    trace_ids: list[str] = []
    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        observations, cursor = await client.get_observations(
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
            if len(trace_ids) >= experiment.item_count:
                break
        if cursor is None or len(trace_ids) >= experiment.item_count:
            break
    else:
        raise LangfuseQueryError(
            0,
            f"Observation pagination exceeded the {_MAX_PAGES}-page safety cap",
        )

    judge_scores: list[ScoreReadData] = []
    for trace_id in trace_ids:
        cursor = None
        for _ in range(_MAX_PAGES):
            scores, cursor = await client.get_scores(
                fields="core,details,subject",
                trace_id=trace_id,
                limit=100,
                cursor=cursor,
            )
            judge_scores.extend(score for score in scores if score.name.startswith("judge_"))
            if cursor is None:
                break
        else:
            raise LangfuseQueryError(
                0,
                f"Score pagination exceeded the {_MAX_PAGES}-page safety cap",
            )
    return judge_scores


async def _fetch_experiment_judge_scores(
    host: str,
    credentials: tuple[str, str],
    experiments: list[dict[str, Any]],
) -> list[list[ScoreReadData] | None]:
    public_key, secret_key = credentials
    client = LangfuseClient(host, public_key, secret_key)
    results: list[list[ScoreReadData] | None] = []
    try:
        for experiment_data in experiments:
            try:
                experiment = CompareExperimentWindow.model_validate(experiment_data)
                scores = await _fetch_posthoc_judge_scores(client, experiment)
            except (httpx.HTTPError, LangfuseQueryError, ValidationError):
                scores = None
            results.append(scores)
    finally:
        await client.close()
    return results


def _buffer_end_time(end_time: str) -> str:
    from datetime import datetime, timedelta

    try:
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except ValueError:
        return end_time
    return (end_dt + timedelta(seconds=2)).isoformat().replace("+00:00", "Z")


async def _fetch_experiment_cost(
    client: LangfuseClient,
    *,
    start_time: str,
    end_time: str,
) -> CostSummary:
    values: list[float] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        scores, cursor = await client.get_scores(
            fields="core,details,subject",
            name="cost_usd",
            from_timestamp=start_time,
            to_timestamp=_buffer_end_time(end_time),
            limit=100,
            cursor=cursor,
        )
        for score in scores:
            match score.value:
                case bool():
                    continue
                case int() as value:
                    values.append(float(value))
                case float() as value:
                    values.append(value)
                case unreachable:
                    assert_never(unreachable)
        if cursor is None:
            break

    total = fsum(values)
    return CostSummary(
        total_usd=total,
        mean_usd=total / len(values) if values else 0.0,
        count=len(values),
    )


async def _fetch_experiment_costs(
    host: str,
    credentials: tuple[str, str],
    experiments: list[dict[str, Any]],
) -> list[CostSummary | None]:
    public_key, secret_key = credentials
    client = LangfuseClient(host, public_key, secret_key)
    summaries: list[CostSummary | None] = []
    try:
        for experiment in experiments:
            try:
                summary = await _fetch_experiment_cost(
                    client,
                    start_time=experiment["startTime"],
                    end_time=experiment["endTime"],
                )
            except (httpx.HTTPError, LangfuseQueryError, ValidationError):
                summary = None
            summaries.append(summary)
    finally:
        await client.close()
    return summaries


# --- setup-judge -------------------------------------------------------------


_JUDGE_TIMEOUT = httpx.Timeout(30.0)
_VALID_TARGETS: dict[str, str] = {"chat": "GENERATION", "agent": "AGENT"}
_VALID_DATA_TYPES: frozenset[str] = frozenset({"NUMERIC", "BOOLEAN", "CATEGORICAL"})


@app.command(
    name="setup-judge",
    help="Configure an LLM-as-a-Judge evaluator + rule via the unstable-evaluators API.",
)
def setup_judge(
    name: Annotated[str, typer.Option("--name", help="Evaluator name (e.g. 'helpfulness').")],
    prompt: Annotated[str, typer.Option("--prompt", help="Judge rubric prompt.")],
    target: Annotated[
        str,
        typer.Option(
            "--target",
            help="Observation type to evaluate: 'chat' (GENERATION) or 'agent' (AGENT).",
        ),
    ] = "chat",
    sampling: Annotated[
        float,
        typer.Option("--sampling", help="Sampling rate between 0.0 and 1.0."),
    ] = 0.1,
    data_type: Annotated[
        str,
        typer.Option("--data-type", help="Score data type: NUMERIC, BOOLEAN, or CATEGORICAL."),
    ] = "NUMERIC",
) -> None:
    """Create an LLM-as-a-Judge evaluator and an evaluation rule.

    Uses the Langfuse v4 ``unstable-evaluators`` and
    ``unstable-evaluation-rules`` API endpoints. These endpoints are marked
    unstable and may change between releases -- this command is a convenience
    wrapper, not a stable interface.
    """
    target_key = target.lower()
    if target_key not in _VALID_TARGETS:
        typer.echo(
            f"ERROR: --target must be one of {sorted(_VALID_TARGETS)}, got '{target}'.",
            err=True,
        )
        raise typer.Exit(code=1)
    observation_type = _VALID_TARGETS[target_key]

    data_type_upper = data_type.upper()
    if data_type_upper not in _VALID_DATA_TYPES:
        typer.echo(
            f"ERROR: --data-type must be one of {sorted(_VALID_DATA_TYPES)}, got '{data_type}'.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not 0.0 <= sampling <= 1.0:
        typer.echo(
            f"ERROR: --sampling must be between 0.0 and 1.0, got {sampling}.",
            err=True,
        )
        raise typer.Exit(code=1)

    host, public_key, secret_key = _load_langfuse_env()
    headers = _basic_auth_header(public_key, secret_key)
    base_url = host.rstrip("/")

    evaluator_body: dict[str, object] = {
        "type": "llm_as_judge",
        "name": name,
        "prompt": prompt,
        "outputDefinition": {
            "dataType": data_type_upper,
            "reasoning": {"description": "Explain your rating"},
            "score": {"description": "Score from 0.0 to 1.0"},
        },
    }

    evaluator = _post_json(
        url=f"{base_url}/api/public/unstable/evaluators",
        headers=headers,
        body=evaluator_body,
        label="evaluator",
    )
    evaluator_id = evaluator.get("id")
    if not isinstance(evaluator_id, str) or not evaluator_id:
        typer.echo("ERROR: evaluator response did not include a string 'id' field.", err=True)
        raise typer.Exit(code=1)

    mapping = _build_variable_mapping(evaluator.get("variables"))

    rule_body: dict[str, object] = {
        "name": f"{name}-rule",
        "target": "observation",
        "evaluator": {"name": name, "scope": "public"},
        "enabled": True,
        "sampling": sampling,
        "filter": [
            {
                "type": "string",
                "column": "type",
                "operator": "=",
                "value": observation_type,
            }
        ],
        "mapping": mapping,
    }

    rule = _post_json(
        url=f"{base_url}/api/public/unstable/evaluation-rules",
        headers=headers,
        body=rule_body,
        label="evaluation rule",
    )
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        typer.echo("ERROR: rule response did not include a string 'id' field.", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Created evaluator '{name}' (id={evaluator_id})")
    typer.echo(f"Created evaluation rule '{name}-rule' (id={rule_id})")


def _build_variable_mapping(variables: object) -> list[dict[str, str]]:
    """Build the rule variable mapping from the evaluator's declared variables.

    For LLM-as-judge evaluators the standard variables are ``input`` and
    ``output``; each maps to ``observation.<variable>``. Falls back to the
    standard pair when the evaluator response did not declare variables.
    """
    standard = [
        {"variable": "input", "source": "observation.input"},
        {"variable": "output", "source": "observation.output"},
    ]
    if not isinstance(variables, list) or not variables:
        return standard

    mapping: list[dict[str, str]] = []
    for var in variables:
        if not isinstance(var, dict):
            continue
        var_name = var.get("name")
        if isinstance(var_name, str) and var_name:
            mapping.append({"variable": var_name, "source": f"observation.{var_name}"})
    return mapping or standard


def _post_json(
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, object],
    label: str,
) -> dict[str, object]:
    """POST JSON to a Langfuse unstable endpoint and return the response body.

    Prints error details and exits with code 1 on HTTP 4xx/5xx, transport
    failure, or a non-object JSON response.
    """
    try:
        with httpx.Client(timeout=_JUDGE_TIMEOUT) as client:
            response = client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        typer.echo(f"ERROR: transport failure creating {label}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if response.status_code >= 400:
        typer.echo(
            f"ERROR: creating {label} failed (HTTP {response.status_code}): {response.text}",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        payload: object = response.json()
    except ValueError as exc:
        typer.echo(f"ERROR: {label} response was not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if not isinstance(payload, dict):
        typer.echo(f"ERROR: {label} response was not a JSON object.", err=True)
        raise typer.Exit(code=1)

    return payload


@app.command(name="metrics", help="Report capability metrics from Langfuse or FILE traces.")
def metrics(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Workspace root containing .modex data."),
    ] = Path("."),
    days: Annotated[int, typer.Option("--days", help="Number of recent days to include.")] = 7,
) -> None:
    """Render a markdown report from Langfuse, with FILE traces as fallback."""
    from bot.eval.metrics import aggregate
    from modex_agent.trace.langfuse_query import LangfuseClient
    from modex_agent.trace.score_injector import L2ScoreInjector

    credentials = LangfuseCredentials.from_env()
    client: LangfuseClient | None = None
    injector: L2ScoreInjector | None = None
    if credentials is not None:
        host = credentials.host if credentials.host is not None else _DEFAULT_LANGFUSE_HOST
        client = LangfuseClient(host, credentials.public_key, credentials.secret_key)
        injector = L2ScoreInjector(
            ingestion_url=f"{host.rstrip('/')}/api/public/ingestion",
            headers=_basic_auth_header(credentials.public_key, credentials.secret_key),
        )
    typer.echo(
        aggregate(
            workspace,
            days,
            langfuse_client=client,
            score_injector=injector,
        )
    )


_GOLDEN_SYSTEM_PROMPT = (
    "You are a careful evaluation assistant. Follow the user's instructions exactly."
)


@contextmanager
def _stable_golden_message_serialization() -> Iterator[None]:
    from modex_agent.core.message import ChatMessage

    original_to_dict = ChatMessage.to_dict

    def stable_to_dict(message: ChatMessage) -> dict[str, Any]:
        serialized = original_to_dict(message)
        serialized.pop("created_at", None)
        return serialized

    with patch.object(ChatMessage, "to_dict", stable_to_dict):
        yield


def _golden_provider_from_env() -> LLMProvider:
    api_key = os.environ.get("TEST_LLM_API_KEY")
    base_url = os.environ.get("TEST_LLM_BASE_URL")
    model = os.environ.get("TEST_LLM_MODEL")
    if not api_key or not base_url or not model:
        typer.echo(
            "ERROR: TEST_LLM_API_KEY, TEST_LLM_BASE_URL, and TEST_LLM_MODEL "
            "environment variables are required.",
            err=True,
        )
        raise typer.Exit(code=1)

    return create_llm_provider(
        LLMConfig(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=float(os.environ.get("TEST_LLM_TEMPERATURE", "0.7")),
            max_output_tokens=int(os.environ.get("TEST_LLM_MAX_OUTPUT_TOKENS", "2000")),
            reasoning_effort=ReasoningEffort(
                os.environ.get("TEST_LLM_REASONING_EFFORT", ReasoningEffort.NONE.value)
            ),
        )
    )


async def _record_golden_case(case_dir: Path) -> None:
    import hashlib
    import json
    import shutil
    import sys
    import tempfile
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from bot.eval import experiment_runner as experiment_runner_module
    from bot.eval.agent_harness import (
        assemble_harness_agent,
        build_runtime_services,
        build_trace_only_services,
        static_system_prompt,
    )
    from bot.eval.replay import GoldenMeta
    from bot.eval.task_output import EvalTaskOutput
    from bot.eval.task_spec import EvalItemSpec
    from modex_agent.core.emitter import StopReason
    from modex_agent.runtime.services import AgentRuntimeServices
    from modex_agent.trace.cassette import (
        CassetteCategory,
        CassetteManifest,
        CassetteRecorder,
    )

    spec = EvalItemSpec.model_validate_json((case_dir / "item.json").read_text(encoding="utf-8"))
    provider = _golden_provider_from_env()
    model = provider.get_default_model()

    with tempfile.TemporaryDirectory(prefix=f"modex-record-{case_dir.name}-") as raw_dir:
        recording_root = Path(raw_dir)
        recorder = CassetteRecorder(recording_root)
        original_build_runtime_services = experiment_runner_module.build_runtime_services

        def recording_runtime_services(
            trace_dir: Path,
            recorder: CassetteRecorder | None = None,
            *,
            model: str | None = None,
        ) -> AgentRuntimeServices:
            _ = recorder
            return build_runtime_services(
                trace_dir=trace_dir,
                recorder=active_recorder,
                model=model,
            )

        active_recorder = recorder
        experiment_runner_module.build_runtime_services = recording_runtime_services
        try:
            with _stable_golden_message_serialization():
                runner = EvalRunner(
                    provider=provider,
                    system_prompt=_GOLDEN_SYSTEM_PROMPT,
                    mode="production",
                    recorder=recorder,
                    model=model,
                )
                raw_output = await runner.task(
                    item=SimpleNamespace(id=spec.id, input=spec.model_dump(mode="json"))
                )
        finally:
            experiment_runner_module.build_runtime_services = original_build_runtime_services

        output = EvalTaskOutput.model_validate(raw_output)
        clean_turns = len(output.turn_records) == len(spec.turns) and all(
            turn.error is None and turn.stop_reason is StopReason.COMPLETED
            for turn in output.turn_records
        )
        world_ok = len(output.world_results) == len(spec.world_assertions) and all(
            result.passed for result in output.world_results
        )
        if not clean_turns or not world_ok or output.stop_mismatches:
            typer.echo(output.model_dump_json(indent=2), err=True)
            raise typer.Exit(code=1)

        cassette_dirs = sorted(
            path
            for path in recording_root.iterdir()
            if path.is_dir() and (path / "index.json").is_file()
        )
        if not cassette_dirs:
            typer.echo("ERROR: recording produced no cassette trace directories.", err=True)
            raise typer.Exit(code=1)

        first_request = None
        for cassette_dir in cassette_dirs:
            manifest = CassetteManifest.model_validate_json(
                (cassette_dir / "index.json").read_text(encoding="utf-8")
            )
            first_request = next(
                (
                    entry.data["request"]
                    for entry in manifest.entries
                    if entry.category is CassetteCategory.LLM_CALL
                ),
                None,
            )
            if first_request is not None:
                break
        if first_request is None:
            typer.echo("ERROR: recording produced no LLM request.", err=True)
            raise typer.Exit(code=1)

        fingerprint_services = build_trace_only_services(
            recording_root / "fingerprint-trace",
            model=model,
        )
        fingerprint_assembly = await assemble_harness_agent(
            workspace=case_dir,
            data_dir=recording_root / "fingerprint-runtime",
            provider=provider,
            toolset=spec.toolset,
            deny_tools=spec.deny_tools,
            runtime_services=fingerprint_services,
            governance_enabled=False,
        )
        tool_manager = fingerprint_assembly.tool_manager
        schemas = sorted(
            tool_manager.get_tool_descriptions(),
            key=lambda item: str(item["function"]["name"]),
        )
        canonical_schemas = json.dumps(
            schemas,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        system_prompt = static_system_prompt(_GOLDEN_SYSTEM_PROMPT)
        meta = GoldenMeta(
            model=str(first_request.get("model") or model),
            temperature=float(first_request["temperature"]),
            tool_names=sorted(tool_manager.list_tools()),
            tool_schema_sha256=hashlib.sha256(canonical_schemas.encode("utf-8")).hexdigest(),
            prompt_sha256=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
            platform=sys.platform,
            recorded_at=datetime.now(UTC).isoformat(),
            baseline=case_dir.name == "chat-notools",
        )
        await fingerprint_assembly.close()

        target_root = case_dir / "cassette"
        if target_root.exists():
            shutil.rmtree(target_root)
        target_root.mkdir(parents=True)
        for cassette_dir in cassette_dirs:
            shutil.copytree(cassette_dir, target_root / cassette_dir.name)
        (case_dir / "meta.json").write_text(
            meta.model_dump_json(indent=2, exclude_defaults=True),
            encoding="utf-8",
        )
        typer.echo(meta.model_dump_json(indent=2, exclude_defaults=True))
        typer.echo(output.model_dump_json(indent=2))


@app.command(name="record-golden", help="Record one golden case with the configured real LLM.")
def record_golden(
    case: Annotated[Path, typer.Option("--case", help="Golden case directory.")],
) -> None:
    asyncio.run(_record_golden_case(case.resolve()))


@app.command(name="replay-golden", help="Replay one golden case without provider credentials.")
def replay_golden(
    case: Annotated[Path, typer.Option("--case", help="Golden case directory.")],
) -> None:
    from bot.eval.replay import GoldenCase, GoldenMeta, GoldenReplayConfig, GoldenReplayRunner

    case_dir = case.resolve()
    meta = GoldenMeta.model_validate_json((case_dir / "meta.json").read_text(encoding="utf-8"))
    runner = GoldenReplayRunner(
        GoldenReplayConfig(
            model=meta.model,
            temperature=meta.temperature,
            system_prompt=_GOLDEN_SYSTEM_PROMPT,
        )
    )
    with _stable_golden_message_serialization():
        result = asyncio.run(runner.run_case(GoldenCase(name=case_dir.name, dir=case_dir)))
    typer.echo(result.model_dump_json(indent=2))
    if not result.passed:
        raise typer.Exit(code=1)


def main() -> None:
    """Entry point for ``python -m bot.eval.cli``."""
    app()


if __name__ == "__main__":
    main()
