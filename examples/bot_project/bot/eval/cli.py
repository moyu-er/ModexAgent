"""Eval harness CLI -- curate datasets, run experiments, compare runs.

Layer 2 of the eval architecture (ADR-0024). Opt-in via the ``[eval]`` extra.
Runs as a separate process to avoid OTel tracer-provider conflicts with the
bot's JSON-OTLP trace path.

Langfuse credentials are read from the environment:

- ``LANGFUSE_HOST`` (default: ``http://localhost:3000``)
- ``LANGFUSE_PUBLIC_KEY`` (required)
- ``LANGFUSE_SECRET_KEY`` (required)

Usage::

    python -m bot.eval.cli curate --dataset react-baseline --max 50
    python -m bot.eval.cli run --dataset react-baseline --experiment v1 --model openai/gpt-4o
    python -m bot.eval.cli compare --dataset react-baseline
    python -m bot.eval.cli setup-judge --name helpfulness \\
        --prompt "Score the helpfulness of the response from 0.0 to 1.0."
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections import defaultdict
from typing import Annotated

import httpx
import typer
from langfuse import Langfuse

from bot.eval.dataset_curator import DatasetCurator
from bot.eval.evaluators import (
    accuracy_evaluator,
    completion_evaluator,
    response_length_evaluator,
)
from bot.eval.experiment_runner import EvalRunner

app = typer.Typer(
    name="bot-eval",
    help="Eval harness -- curate datasets, run experiments, compare runs.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

_DEFAULT_LANGFUSE_HOST = "http://localhost:3000"
_SCORES_TIMEOUT = httpx.Timeout(10.0)


# --- Langfuse credentials ----------------------------------------------------


def _load_langfuse_env() -> tuple[str, str, str]:
    """Read Langfuse credentials from the environment.

    Returns ``(host, public_key, secret_key)``. Exits with code 1 when
    ``LANGFUSE_PUBLIC_KEY`` or ``LANGFUSE_SECRET_KEY`` is missing.
    """
    host = os.environ.get("LANGFUSE_HOST", _DEFAULT_LANGFUSE_HOST)
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        typer.echo(
            "ERROR: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY environment "
            "variables are required.",
            err=True,
        )
        raise typer.Exit(code=1)
    return host, public_key, secret_key


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
        typer.Option("--filter-errors", help="Include traces that errored."),
    ] = True,
    filter_high_latency: Annotated[
        bool,
        typer.Option("--filter-high-latency", help="Include high-latency traces."),
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
        typer.Option("--model", help="LiteLLM model string, e.g. openai/gpt-4o."),
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
) -> None:
    """Run an experiment against a Langfuse dataset.

    The LLM API key is read from the standard environment variable for the
    model's provider (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, etc.) -- set
    it before invoking this command.
    """
    # Lazy import: litellm is an optional dependency (the [llm] extra), not
    # required for curate/compare.
    from modex_agent.providers import LiteLLMProvider

    host, public_key, secret_key = _load_langfuse_env()
    provider = LiteLLMProvider(model=model)
    langfuse_client = Langfuse(
        host=host,
        public_key=public_key,
        secret_key=secret_key,
    )
    runner = EvalRunner(
        provider=provider,
        system_prompt=system_prompt,
        max_iterations=max_iterations,
        langfuse_client=langfuse_client,
    )
    result = runner.run(
        dataset_name=dataset,
        experiment_name=experiment,
        evaluators=[
            accuracy_evaluator,
            completion_evaluator,
            response_length_evaluator,
        ],
        max_concurrency=max_concurrency,
    )
    typer.echo(result.format())


# --- compare -----------------------------------------------------------------


@app.command(name="compare", help="Compare experiment runs for a dataset.")
def compare(
    dataset: Annotated[str, typer.Option("--dataset", help="Dataset to compare runs for.")],
) -> None:
    """List experiment runs for a dataset with their aggregated scores."""
    host, public_key, secret_key = _load_langfuse_env()
    lf = Langfuse(host=host, public_key=public_key, secret_key=secret_key)
    headers = _basic_auth_header(public_key, secret_key)

    paginated = lf.get_dataset_runs(dataset_name=dataset, limit=100)
    runs = paginated.data
    if not runs:
        typer.echo(f"No experiment runs found for dataset '{dataset}'.")
        return

    typer.echo(f"Experiment runs for dataset '{dataset}':")
    typer.echo()
    typer.echo(f"{'Run':<30} {'Created':<20} {'Scores'}")
    typer.echo(f"{'---':<30} {'---':<20} {'---'}")

    for run in runs:
        created = run.created_at.strftime("%Y-%m-%d %H:%M")
        scores = _fetch_run_scores(
            host=host,
            headers=headers,
            dataset_run_id=run.id,
        )
        typer.echo(f"{run.name:<30} {created:<20} {scores}")


def _fetch_run_scores(
    *,
    host: str,
    headers: dict[str, str],
    dataset_run_id: str,
) -> str:
    """Fetch and aggregate scores for a dataset run via the REST API.

    Queries ``GET /api/public/v2/scores?datasetRunId=...`` and averages each
    score name. Returns a compact summary like ``accuracy=75%, completion=80%``
    or ``"(no scores)"`` / ``"(unavailable)"`` on failure.
    """
    url = f"{host.rstrip('/')}/api/public/v2/scores"
    params = {"datasetRunId": dataset_run_id, "limit": 500}
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
    if not isinstance(data, list) or not data:
        return "(no scores)"

    name_values: dict[str, list[float]] = defaultdict(list)
    for score in data:
        if not isinstance(score, dict):
            continue
        name = score.get("name")
        value = score.get("value")
        if isinstance(name, str) and isinstance(value, int | float):
            name_values[name].append(float(value))

    if not name_values:
        return "(no scores)"

    parts: list[str] = []
    for name, values in sorted(name_values.items()):
        avg = sum(values) / len(values)
        if avg <= 1.0:
            parts.append(f"{name}={avg:.0%}")
        else:
            parts.append(f"{name}={avg:.1f}")
    return ", ".join(parts)


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


def main() -> None:
    """Entry point for ``python -m bot.eval.cli``."""
    app()


if __name__ == "__main__":
    main()
