from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

import pytest
from bot.eval.probes.renderer import ProbeRenderer, ProbeRenderError, RendererConfig
from bot.eval.probes.schema import (
    Fact,
    Persona,
    Probe,
    ProbeType,
    Session,
    SessionTurn,
    Speaker,
    WorldSpec,
)

from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.types import LLMResponse


class ScriptedProvider(CallbackStreamProvider):
    def __init__(self, outputs: list[str]) -> None:
        super().__init__(retry_backoff_seconds=())
        self.outputs = deque(outputs)
        self.calls: list[list[ChatMessage]] = []

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs,
    ) -> LLMResponse:
        del model, temperature, max_output_tokens, tools, kwargs
        self.calls.append(messages)
        return LLMResponse(content=self.outputs.popleft())

    def get_default_model(self) -> str:
        return "scripted-renderer"


def _world() -> WorldSpec:
    timestamp = datetime(2026, 1, 5, tzinfo=UTC)
    fact = Fact(
        fact_id="f-city",
        persona_id="p-a",
        attribute="city",
        value="Oslo",
        valid_from=timestamp,
        surface_refs=["My city is Oslo."],
    )
    return WorldSpec(
        suite_version="v1",
        seed=7,
        personas=[
            Persona(persona_id="p-a", display_name="Ari", traits=["concise"]),
            Persona(persona_id="p-b", display_name="Bo", traits=["patient"]),
        ],
        facts=[fact],
        sessions=[
            Session(
                session_id="s-a",
                persona_id="p-a",
                timestamp=timestamp,
                turns=[
                    SessionTurn(
                        speaker=Speaker.USER,
                        text=fact.surface_refs[0],
                        fact_ids=[fact.fact_id],
                    )
                ],
            )
        ],
        probes=[
            Probe(
                probe_id="probe-city",
                probe_type=ProbeType.EXTRACTION,
                persona_id="p-a",
                question="Canonical city question?",
                expected_answers=["Oslo"],
                fact_ids=[fact.fact_id],
            )
        ],
    )


@pytest.mark.asyncio
async def test_renderer_accepts_valid_conversation_and_question_outputs() -> None:
    provider = ScriptedProvider(
        [
            '{"sentences":[{"fact_id":"f-city","speaker":"user","text":"I moved to Oslo last spring."}]}',
            '{"questions":[{"probe_id":"probe-city","question":"Where did I say I moved?"}]}',
        ]
    )

    result = await ProbeRenderer(provider).render(_world())

    assert result.world.facts[0].surface_refs == ["I moved to Oslo last spring."]
    assert result.world.sessions[0].turns[0].text == "I moved to Oslo last spring."
    assert result.world.probes[0].question == "Where did I say I moved?"
    assert (result.conversation_attempts, result.question_attempts) == (1, 1)
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_renderer_retries_live_flat_map_with_schema_described_prompts() -> None:
    provider = ScriptedProvider(
        [
            '{"f-city":"My project is espresso."}',
            '{"sentences":[{"fact_id":"f-city","speaker":"user","text":"I live in Oslo."}]}',
            '{"questions":[{"probe_id":"probe-city","question":"Where do I live?"}]}',
        ]
    )

    result = await ProbeRenderer(provider).render(_world())

    assert result.conversation_attempts == 2
    conversation_prompt = provider.calls[0][0].content
    question_prompt = provider.calls[2][0].content
    assert isinstance(conversation_prompt, str)
    assert isinstance(question_prompt, str)
    assert '"sentences"' in conversation_prompt
    assert '"questions"' in question_prompt


@pytest.mark.asyncio
async def test_renderer_accepts_markdown_fenced_json_on_both_sides() -> None:
    provider = ScriptedProvider(
        [
            '```json\n{"sentences":[{"fact_id":"f-city","speaker":"user","text":"I live in Oslo."}]}\n```',
            '```json\n{"questions":[{"probe_id":"probe-city","question":"Where do I live?"}]}\n```',
        ]
    )

    result = await ProbeRenderer(provider).render(_world())

    assert (result.conversation_attempts, result.question_attempts) == (1, 1)
    assert result.world.probes[0].question == "Where do I live?"


@pytest.mark.asyncio
async def test_renderer_cannot_overwrite_programmatic_truth() -> None:
    provider = ScriptedProvider(
        [
            '{"sentences":[{"fact_id":"f-city","speaker":"user","text":"Oslo is home.","expected_answers":["Lima"]}]}',
            '{"sentences":[{"fact_id":"f-city","speaker":"user","text":"Oslo is home."}]}',
            '{"questions":[{"probe_id":"probe-city","question":"Where is home?"}]}',
        ]
    )
    original = _world()

    result = await ProbeRenderer(provider).render(original)

    assert result.world.probes[0].expected_answers == original.probes[0].expected_answers
    assert result.world.probes[0].fact_ids == original.probes[0].fact_ids
    assert result.conversation_attempts == 2


@pytest.mark.asyncio
async def test_renderer_retries_malformed_output_then_aborts_loudly() -> None:
    provider = ScriptedProvider(["not-json", "{}", '{"sentences":[]}'])
    renderer = ProbeRenderer(provider, RendererConfig(max_attempts=3))

    with pytest.raises(ProbeRenderError) as caught:
        await renderer.render(_world())

    assert caught.value.side == "conversation"
    assert caught.value.attempts == 3
    assert caught.value.last_output == '{"sentences":[]}'
    assert len(provider.calls) == 3
