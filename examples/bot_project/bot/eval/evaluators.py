"""Item-level and run-level evaluators for Langfuse experiments.

Each item-level evaluator receives ``input``, ``output``, ``expected_output``,
and ``metadata`` from the experiment runner. Run-level evaluators receive
``item_results`` and compute aggregate metrics.

Returns :class:`langfuse.Evaluation` objects which Langfuse attaches as scores
to the corresponding trace or dataset run.

The verify-the-world evaluators (:func:`world_state_evaluator`,
:func:`tool_success_evaluator`) read the typed-model dumps the eval runner
emits in the output dict (``world_results`` / ``tool_stats`` keys), not
agent prose — success is measured against the observable environment, never
against the agent's claims about it.
"""

from __future__ import annotations

from langfuse import Evaluation

__all__ = [
    "accuracy_evaluator",
    "completion_evaluator",
    "response_length_evaluator",
    "world_state_evaluator",
    "tool_success_evaluator",
    "avg_accuracy",
]


def accuracy_evaluator(
    *,
    input: object,
    output: object,
    expected_output: object,
    metadata: dict[str, object] | None = None,
    **kwargs: object,
) -> Evaluation:
    """Check if the agent's output contains the expected answer.

    Expected output should be a dict with an ``answer`` key, or a plain string.
    The check is case-insensitive substring matching — suitable for factual
    Q&A datasets, not for open-ended evaluation.
    """
    output_text = _extract_text(output)
    expected_text = _extract_text(expected_output)

    if not expected_text:
        return Evaluation(
            name="accuracy",
            value=0.0,
            comment="No expected output provided",
        )

    if expected_text.lower() in output_text.lower():
        return Evaluation(
            name="accuracy",
            value=1.0,
            comment="Correct answer found in output",
        )

    return Evaluation(
        name="accuracy",
        value=0.0,
        comment="Expected answer not found in output",
    )


def completion_evaluator(
    *,
    input: object,
    output: object,
    expected_output: object = None,
    metadata: dict[str, object] | None = None,
    **kwargs: object,
) -> Evaluation:
    """Check if the agent completed without error.

    Looks for ``stop_reason`` in the output dict. A completed run
    (``StopReason.COMPLETED``) with no error gets ``True``; anything
    else gets ``False``.
    """
    if not isinstance(output, dict):
        return Evaluation(
            name="completion",
            value=False,
            data_type="BOOLEAN",
            comment="Output is not a dict — cannot check stop_reason",
        )

    stop_reason = str(output.get("stop_reason", ""))
    error = output.get("error")

    is_completed = "completed" in stop_reason.lower() and not error

    return Evaluation(
        name="completion",
        value=is_completed,
        data_type="BOOLEAN",
        comment=f"stop_reason={stop_reason}, error={'yes' if error else 'no'}",
    )


def response_length_evaluator(
    *,
    input: object,
    output: object,
    expected_output: object = None,
    metadata: dict[str, object] | None = None,
    **kwargs: object,
) -> Evaluation:
    """Measure the character length of the agent's response.

    Useful for tracking verbosity across prompt versions. Lower is not
    necessarily better — pair with accuracy for a balanced view.
    """
    output_text = _extract_text(output)
    return Evaluation(
        name="response_length",
        value=len(output_text),
        comment=f"Response has {len(output_text)} characters",
    )


def world_state_evaluator(
    *,
    input: object,
    output: object,
    expected_output: object = None,
    metadata: dict[str, object] | None = None,
    **kwargs: object,
) -> Evaluation:
    """Check every world assertion the eval runner recorded.

    Reads ``world_results`` from the output dict — the typed-model dump
    produced by the runner, not agent prose. Each entry is
    ``{"assertion": str, "passed": bool, "detail": str}``. True only when
    every assertion passed; the comment names the failed assertions.
    """
    if not isinstance(output, dict):
        return Evaluation(
            name="world_state",
            value=False,
            data_type="BOOLEAN",
            comment="Output is not a dict — no world results",
        )

    results = output.get("world_results")
    if not isinstance(results, list):
        return Evaluation(
            name="world_state",
            value=False,
            data_type="BOOLEAN",
            comment="No world results in output",
        )

    failed_labels = [
        str(record["assertion"])
        for record in results
        if isinstance(record, dict) and not record["passed"]
    ]
    if failed_labels:
        return Evaluation(
            name="world_state",
            value=False,
            data_type="BOOLEAN",
            comment="Failed: " + "; ".join(failed_labels),
        )

    return Evaluation(
        name="world_state",
        value=True,
        data_type="BOOLEAN",
        comment=f"All {len(results)} world assertions passed",
    )


def tool_success_evaluator(
    *,
    input: object,
    output: object,
    expected_output: object = None,
    metadata: dict[str, object] | None = None,
    **kwargs: object,
) -> Evaluation:
    """Score the share of tool calls that succeeded.

    Reads ``tool_stats`` from the output dict — the typed-model dump
    produced by the runner, not agent prose. ``total == 0`` scores 1.0
    (nothing failed); a missing stats dump scores 0.0 so silent
    instrumentation loss stays visible instead of reading as success.
    """
    stats = output.get("tool_stats") if isinstance(output, dict) else None
    if not isinstance(stats, dict):
        return Evaluation(
            name="tool_success",
            value=0.0,
            data_type="NUMERIC",
            comment="No tool stats in output",
        )

    total = stats.get("total", 0)
    if total == 0:
        return Evaluation(
            name="tool_success",
            value=1.0,
            data_type="NUMERIC",
            comment="No tool calls",
        )

    success_rate = float(stats["success_rate"])
    return Evaluation(
        name="tool_success",
        value=success_rate,
        data_type="NUMERIC",
        comment=f"{stats.get('errors', 0)} of {total} tool calls errored",
    )


def avg_accuracy(
    *,
    item_results: list[object],
    **kwargs: object,
) -> Evaluation:
    """Run-level evaluator: average accuracy across all items.

    Aggregates the ``accuracy`` score from each item's evaluations.
    Returns a single NUMERIC score representing overall accuracy.
    """
    accuracies: list[float] = []
    for result in item_results:
        evaluations = getattr(result, "evaluations", []) or []
        for evaluation in evaluations:
            eval_name = getattr(evaluation, "name", "")
            eval_value = getattr(evaluation, "value", None)
            if eval_name == "accuracy" and isinstance(eval_value, int | float):
                accuracies.append(float(eval_value))

    if not accuracies:
        return Evaluation(
            name="avg_accuracy",
            value=0.0,
            comment="No accuracy scores found in item results",
        )

    average = sum(accuracies) / len(accuracies)
    return Evaluation(
        name="avg_accuracy",
        value=average,
        comment=f"Average accuracy across {len(accuracies)} items: {average:.2%}",
    )


def _extract_text(value: object) -> str:
    """Extract a text string from an experiment value.

    Handles dicts with ``output`` or ``answer`` keys, plain strings,
    and falls back to ``str()`` for other types.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("output", "answer", "content", "text"):
            if key in value and isinstance(value[key], str):
                return value[key]
    return str(value) if value is not None else ""
