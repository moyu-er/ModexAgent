"""Core behavior tests for the rubric judge runner."""

from __future__ import annotations

from collections.abc import Sequence
from typing import assert_never

import pytest
from bot.eval.judge.rubrics import Rubric, RubricSet, rubric_version
from bot.eval.judge.runner import (
    JudgeInput,
    JudgeRunner,
    Verdict,
)

from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.runtime.models import JsonValue


class ScriptedProviderError(RuntimeError):
    """Expected provider failure used by the scripted test provider."""


class RecordingProvider(CallbackStreamProvider):
    """Mutable scripted provider that records the request-level controls."""

    def __init__(
        self,
        responses: Sequence[str | LLMResponse | ScriptedProviderError],
    ) -> None:
        super().__init__()
        self._responses = list(responses)
        self.temperatures: list[float] = []
        self.seeds: list[int | None] = []
        self.messages: list[list[ChatMessage]] = []

    def get_default_model(self) -> str:
        return "judge/test-model"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, JsonValue]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        seed: int | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        del model, max_output_tokens, tools, kwargs
        self.messages.append(messages)
        self.temperatures.append(temperature)
        self.seeds.append(seed)
        scripted = self._responses.pop(0)
        match scripted:
            case str() as content:
                return LLMResponse(content=content, finish_reason=FinishReason.STOP)
            case LLMResponse() as response:
                return response
            case ScriptedProviderError() as error:
                raise error
            case unreachable:
                assert_never(unreachable)


@pytest.fixture
def rubric_set() -> RubricSet:
    return RubricSet(
        name="runner-test",
        rubrics=[
            Rubric(
                criterion="completion",
                description="The requested outcome is complete.",
                weight=0.75,
            ),
            Rubric(
                criterion="verification",
                description="The outcome is empirically verified.",
                weight=0.25,
            ),
        ],
    )


def _judge_input(rubric_set: RubricSet) -> JudgeInput:
    return JudgeInput(
        item_context="Create and verify the requested artifact.",
        rubric_set=rubric_set,
        agent_output="Created the artifact and ran its checks.",
        trace_id="candidate-trace",
        session_id="eval-session",
    )


async def test_valid_json_returns_exact_verdicts_and_weighted_score(
    rubric_set: RubricSet,
) -> None:
    # Given: a single code-fenced, valid rubric verdict response.
    provider = RecordingProvider(
        [
            """```json
            {"verdicts":[
              {"criterion":"completion","verdict":"MET","evidence":"Created the artifact."},
              {"criterion":"verification","verdict":"UNMET","evidence":"No check output quoted."}
            ],"summary":"Complete but not evidenced."}
            ```"""
        ]
    )

    # When: the runner performs one review.
    result = await JudgeRunner(provider).review(_judge_input(rubric_set))

    # Then: verdict ordering and the hand-computed 0.75 score are exact.
    assert result.parse_ok is True
    assert [(item.criterion, item.verdict) for item in result.verdicts] == [
        ("completion", Verdict.MET),
        ("verification", Verdict.UNMET),
    ]
    assert result.weighted_score == pytest.approx(0.75)
    assert result.na_count == 0
    assert result.summary == "Complete but not evidenced."
    assert result.provenance.rubric_version == rubric_version(rubric_set)
    assert result.provenance.seed_applied is True
    assert provider.temperatures == [0.0]
    assert provider.seeds == [0]
    assert len(provider.messages) == 1


async def test_na_counts_as_zero_in_weighted_score(rubric_set: RubricSet) -> None:
    # Given: a response that marks the high-weight criterion NA and the other MET.
    provider = RecordingProvider(
        [
            '{"verdicts":['
            '{"criterion":"completion","verdict":"NA","evidence":"Not applicable."},'
            '{"criterion":"verification","verdict":"MET","evidence":"Checks passed."}'
            '],"summary":"Only verification applies."}'
        ]
    )

    # When: the result is aggregated.
    result = await JudgeRunner(provider).review(_judge_input(rubric_set))

    # Then: NA remains in the denominator, contributes zero, and is counted.
    assert result.weighted_score == pytest.approx(0.25)
    assert result.na_count == 1
    assert [item.verdict for item in result.verdicts] == [Verdict.NA, Verdict.MET]


async def test_cannot_assess_is_excluded_from_weighted_score(
    rubric_set: RubricSet,
) -> None:
    # Given: one criterion is met and the other cannot be assessed.
    provider = RecordingProvider(
        [
            '{"verdicts":['
            '{"criterion":"completion","verdict":"MET","evidence":"Created."},'
            '{"criterion":"verification","verdict":"CANNOT_ASSESS","evidence":"No logs."}'
            '],"summary":"Verification unavailable."}'
        ]
    )

    # When: the result is aggregated.
    result = await JudgeRunner(provider).review(_judge_input(rubric_set))

    # Then: only the assessable criterion contributes to the denominator.
    assert result.weighted_score == pytest.approx(1.0)
    assert result.na_count == 0


async def test_malformed_output_marks_whole_result_cannot_assess(
    rubric_set: RubricSet,
) -> None:
    # Given: a truncated JSON object from the provider.
    raw_output = '{"verdicts":[{"criterion":"completion"'
    provider = RecordingProvider([raw_output])

    # When: parsing cannot recover a complete object.
    result = await JudgeRunner(provider).review(_judge_input(rubric_set))

    # Then: every criterion fails closed and the raw evidence is preserved.
    assert result.parse_ok is False
    assert all(item.verdict == Verdict.CANNOT_ASSESS for item in result.verdicts)
    assert result.weighted_score == 0.0
    assert result.raw_output == raw_output
    assert result.provenance.seed_applied is True
    assert all(item.verdict != Verdict.MET for item in result.verdicts)


async def test_provider_exception_never_produces_met(rubric_set: RubricSet) -> None:
    # Given: the judge API raises before returning a response.
    provider = RecordingProvider([ScriptedProviderError("judge endpoint unavailable")])

    # When: the runner handles the API failure boundary.
    result = await JudgeRunner(provider).review(_judge_input(rubric_set))

    # Then: the failure is surfaced and all criteria fail closed.
    assert result.parse_ok is False
    assert "judge endpoint unavailable" in result.raw_output
    assert all(item.verdict == Verdict.CANNOT_ASSESS for item in result.verdicts)
    assert result.provenance.seed_applied is False


async def test_provider_error_response_never_produces_met(
    rubric_set: RubricSet,
) -> None:
    # Given: the judge API returns its typed error outcome.
    provider = RecordingProvider(
        [
            LLMResponse(
                content=None,
                finish_reason=FinishReason.ERROR,
                error="judge request rejected",
            )
        ]
    )

    # When: the runner handles the error response boundary.
    result = await JudgeRunner(provider).review(_judge_input(rubric_set))

    # Then: every criterion fails closed and the API error is preserved.
    assert result.parse_ok is False
    assert result.raw_output == "judge request rejected"
    assert all(item.verdict == Verdict.CANNOT_ASSESS for item in result.verdicts)
    assert result.provenance.seed_applied is False
