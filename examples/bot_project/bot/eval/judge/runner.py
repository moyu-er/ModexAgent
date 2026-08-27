"""Single-call rubric judging with deterministic controls and audit tracing."""

from __future__ import annotations

import inspect
import logging
import os
import time
from collections import Counter
from typing import Final, assert_never
from uuid import uuid4

from bot.eval.judge._models import (
    JudgeConfig,
    JudgeInput,
    JudgeProvenance,
    JudgeResult,
    JudgeVerdict,
    Verdict,
)
from bot.eval.judge._verdicts import failure_result, result_from_output
from modex_agent.core.constants import FinishReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.types import MessageRole
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanStatusCode
from modex_agent.trace.store import SpanModel, SpanStatus

logger = logging.getLogger(__name__)

_TEMPERATURE: Final = 0.0
_DEFAULT_SEED: Final = 0

JUDGE_PROMPT_TEMPLATE: Final = """You are a strict rubric judge.
Judge each criterion independently from only the supplied context and agent output.
Return exactly one JSON object with this shape:
{{"verdicts":[{{"criterion":"name","verdict":"MET|UNMET|NA|CANNOT_ASSESS","evidence":"one-line exact quote"}}],"summary":"one line"}}
Return every listed criterion once. Do not include markdown or commentary.

RUBRICS (aggregation weights are intentionally hidden):
{rubrics}

ITEM CONTEXT:
{item_context}

AGENT OUTPUT:
{agent_output}
"""


class JudgeConfigurationError(RuntimeError):
    """A required independent judge setting is absent."""

    def __init__(self, variable: str) -> None:
        self.variable = variable
        super().__init__(
            f"{variable} is required to build the judge provider; "
            "the answer model is never used as a fallback"
        )


def build_judge_provider_from_env() -> LLMProvider:
    """Build the independent judge provider with construction-time t=0."""
    model = os.environ.get("JUDGE_MODEL")
    if not model:
        raise JudgeConfigurationError("JUDGE_MODEL")
    return create_llm_provider(
        LLMConfig(
            model=model,
            api_key=os.environ.get("JUDGE_API_KEY") or "",
            base_url=os.environ.get("JUDGE_BASE_URL") or "",
            temperature=_TEMPERATURE,
        )
    )


class JudgeRunner:
    """Run one blocking rubric review per input without retaining review state.

    ``JUDGE_MODEL`` is intentionally mandatory. Ticket 12 section ❸'s model
    separation rule supersedes ticket 03's earlier answer-model fallback line;
    this runner must never restore that fallback.
    """

    def __init__(
        self,
        provider: LLMProvider,
        config: JudgeConfig | None = None,
        *,
        trace_store: OtelSpanTraceStore | None = None,
    ) -> None:
        self._provider = provider
        self._config = config or JudgeConfig()
        self._trace_store = trace_store

    async def review(self, judge_input: JudgeInput) -> JudgeResult:
        """Review one candidate with one awaited provider call and emit one span."""
        started_at = time.time()
        prompt = _render_prompt(judge_input)
        model = self._provider.get_default_model()
        seed_applied = _accepts_seed(self._provider)
        try:
            messages = [ChatMessage(role=MessageRole.USER, content=prompt)]
            if seed_applied:
                response = await self._provider.chat(
                    messages,
                    model=model,
                    temperature=_TEMPERATURE,
                    max_output_tokens=self._config.max_output_tokens,
                    seed=self._config.seed,
                )
            else:
                response = await self._provider.chat(
                    messages,
                    model=model,
                    temperature=_TEMPERATURE,
                    max_output_tokens=self._config.max_output_tokens,
                )
        except Exception as error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            result = failure_result(
                judge_input.rubric_set,
                model,
                f"Judge provider error ({type(error).__name__}): {error}",
            )
        else:
            match response.finish_reason:
                case FinishReason.ERROR:
                    raw_output = response.error or response.content or "Judge provider returned an error"
                    result = failure_result(judge_input.rubric_set, model, raw_output)
                case (
                    FinishReason.STOP
                    | FinishReason.TOOL_CALLS
                    | FinishReason.LENGTH
                    | FinishReason.CONTENT_FILTER
                    | FinishReason.CANCELLED
                ):
                    raw_output = response.content or ""
                    result = result_from_output(
                        judge_input.rubric_set,
                        model,
                        raw_output,
                        seed_applied=seed_applied,
                    )
                case unreachable:
                    assert_never(unreachable)
        await self._emit_span(judge_input, prompt, result, started_at)
        return result

    async def _emit_span(
        self,
        judge_input: JudgeInput,
        prompt: str,
        result: JudgeResult,
        started_at: float,
    ) -> None:
        if self._trace_store is None:
            return
        distribution: Counter[str] = Counter(item.verdict for item in result.verdicts)
        attributes: dict[str, str | int | float] = {
            "judge_model": result.provenance.judge_model,
            "rubric_version": result.provenance.rubric_version,
            "verdict_met_count": distribution[Verdict.MET],
            "verdict_unmet_count": distribution[Verdict.UNMET],
            "verdict_na_count": distribution[Verdict.NA],
            "verdict_cannot_assess_count": distribution[Verdict.CANNOT_ASSESS],
            "weighted_score": result.weighted_score,
            GenAiAttr.LANGFUSE_TRACE_INPUT: prompt,
            GenAiAttr.LANGFUSE_TRACE_OUTPUT: result.raw_output,
        }
        if judge_input.trace_id is not None:
            attributes["candidate_trace_id"] = judge_input.trace_id
        if judge_input.session_id is not None:
            attributes[GenAiAttr.CONVERSATION_ID] = judge_input.session_id
        finished_at = time.time()
        span = SpanModel(
            trace_id=uuid4().hex,
            span_id=uuid4().hex[:16],
            parent_span_id=None,
            name="judge.review",
            kind=SpanKind.INTERNAL,
            start_time=started_at,
            end_time=finished_at,
            attributes=attributes,
            status=SpanStatus(
                code=SpanStatusCode.OK if result.parse_ok else SpanStatusCode.ERROR
            ),
        )
        try:
            await self._trace_store.save_span(span)
        except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            logger.warning("Judge runner failed to save judge.review span", exc_info=True)


def _render_prompt(judge_input: JudgeInput) -> str:
    rubrics = "\n".join(
        f"- {rubric.criterion}: {rubric.description}"
        for rubric in judge_input.rubric_set.rubrics
    )
    return JUDGE_PROMPT_TEMPLATE.format(
        rubrics=rubrics,
        item_context=judge_input.item_context,
        agent_output=judge_input.agent_output,
    )


def _accepts_seed(provider: LLMProvider) -> bool:
    parameters = inspect.signature(provider.chat).parameters.values()
    return any(
        parameter.name == "seed" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


__all__ = [
    "JUDGE_PROMPT_TEMPLATE",
    "JudgeConfig",
    "JudgeConfigurationError",
    "JudgeInput",
    "JudgeProvenance",
    "JudgeResult",
    "JudgeRunner",
    "JudgeVerdict",
    "Verdict",
    "build_judge_provider_from_env",
]
