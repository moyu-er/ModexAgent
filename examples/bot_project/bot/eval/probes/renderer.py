"""Schema-bounded LLM surface rendering that cannot alter probe truth."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.eval.probes.schema import Session, SessionTurn, Speaker, WorldSpec
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.types import MessageRole


class RenderSide(StrEnum):
    """The independently retried rendering boundaries."""

    CONVERSATION = "conversation"
    QUESTION = "question"


class RendererConfig(BaseModel):
    """Limits for deterministic renderer control flow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=3, ge=1)
    max_output_tokens: int = Field(default=16_000, ge=1)
    temperature: float = Field(default=0.2, ge=0, le=2)


class RenderedFactSentence(BaseModel):
    """One rendered fact sentence; no truth field is accepted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(min_length=1)
    speaker: Speaker
    text: str = Field(min_length=1)


class ConversationRender(BaseModel):
    """Strict conversation-side renderer response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sentences: list[RenderedFactSentence]


class RenderedQuestion(BaseModel):
    """One rendered question with no answer channel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str = Field(min_length=1)
    question: str = Field(min_length=1)


class QuestionRender(BaseModel):
    """Strict question-side renderer response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    questions: list[RenderedQuestion]


_CONVERSATION_SCHEMA: Final = json.dumps(
    ConversationRender.model_json_schema(),
    ensure_ascii=True,
    separators=(",", ":"),
)
_QUESTION_SCHEMA: Final = json.dumps(
    QuestionRender.model_json_schema(),
    ensure_ascii=True,
    separators=(",", ":"),
)


class RenderResult(BaseModel):
    """Rendered world plus observable retry counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    world: WorldSpec
    conversation_attempts: int = Field(ge=1)
    question_attempts: int = Field(ge=1)


class ProbeRenderError(RuntimeError):
    """A rendering side exhausted its schema-validation attempts."""

    def __init__(self, side: RenderSide, attempts: int, last_output: str) -> None:
        self.side = side
        self.attempts = attempts
        self.last_output = last_output
        super().__init__(
            f"{side.value} rendering failed schema validation after {attempts} attempts; "
            f"last output: {last_output!r}"
        )


class _RenderShapeError(ValueError):
    pass


class ProbeRenderer:
    """Render conversations and questions while retaining sampled truth verbatim."""

    def __init__(
        self,
        provider: LLMProvider,
        config: RendererConfig | None = None,
    ) -> None:
        self._provider = provider
        self._config = config or RendererConfig()

    async def render(self, world: WorldSpec) -> RenderResult:
        """Render both surfaces, retrying malformed output independently."""
        conversation, conversation_attempts = await self._render_conversation(world)
        conversation_world = _apply_conversation(world, conversation)
        questions, question_attempts = await self._render_questions(conversation_world)
        return RenderResult(
            world=_apply_questions(conversation_world, questions),
            conversation_attempts=conversation_attempts,
            question_attempts=question_attempts,
        )

    async def _render_conversation(
        self,
        world: WorldSpec,
    ) -> tuple[ConversationRender, int]:
        expected_ids = {fact.fact_id for fact in world.facts}
        prompt = (
            "Render each supplied fact as one natural dialogue sentence. Return only JSON "
            "matching the target schema. Preserve every fact_id exactly and add no ids.\n"
            f"Target JSON schema: {_CONVERSATION_SCHEMA}\nInput: "
            + world.model_dump_json(
                include={"personas": True, "facts": True},
            )
        )
        for attempt in range(1, self._config.max_attempts + 1):
            output = await self._chat(prompt)
            try:
                parsed = _parse_render(ConversationRender, output)
                actual_ids = [sentence.fact_id for sentence in parsed.sentences]
                if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected_ids:
                    raise _RenderShapeError("conversation fact ids differ from sampled facts")
            except (ValidationError, _RenderShapeError):
                if attempt == self._config.max_attempts:
                    raise ProbeRenderError(RenderSide.CONVERSATION, attempt, output) from None
            else:
                return parsed, attempt
        raise AssertionError("renderer attempt range is non-empty")

    async def _render_questions(self, world: WorldSpec) -> tuple[QuestionRender, int]:
        expected_ids = {probe.probe_id for probe in world.probes}
        prompt = (
            "Rewrite each supplied probe question naturally. Return only JSON matching "
            "the target schema. Preserve probe_id; do not return answers or truth fields.\n"
            f"Target JSON schema: {_QUESTION_SCHEMA}\nInput: "
            + world.model_dump_json(include={"personas": True, "probes": True})
        )
        for attempt in range(1, self._config.max_attempts + 1):
            output = await self._chat(prompt)
            try:
                parsed = _parse_render(QuestionRender, output)
                actual_ids = [question.probe_id for question in parsed.questions]
                if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected_ids:
                    raise _RenderShapeError("question probe ids differ from sampled probes")
            except (ValidationError, _RenderShapeError):
                if attempt == self._config.max_attempts:
                    raise ProbeRenderError(RenderSide.QUESTION, attempt, output) from None
            else:
                return parsed, attempt
        raise AssertionError("renderer attempt range is non-empty")

    async def _chat(self, prompt: str) -> str:
        response = await self._provider.chat(
            [ChatMessage(role=MessageRole.USER, content=prompt)],
            model=self._provider.get_default_model(),
            temperature=self._config.temperature,
            max_output_tokens=self._config.max_output_tokens,
        )
        return response.content or ""


def _parse_render[RenderModelT: BaseModel](
    model: type[RenderModelT],
    output: str,
) -> RenderModelT:
    try:
        return model.model_validate_json(output)
    except ValidationError:
        return model.model_validate_json(_strip_markdown_fence(output))


def _strip_markdown_fence(output: str) -> str:
    stripped = output.strip()
    opening, separator, remainder = stripped.partition("\n")
    if (
        separator
        and opening.strip().casefold() in {"```", "```json"}
        and remainder.rstrip().endswith("```")
    ):
        return remainder.rstrip()[:-3].strip()
    return stripped


def _apply_conversation(world: WorldSpec, rendered: ConversationRender) -> WorldSpec:
    sentences = {sentence.fact_id: sentence for sentence in rendered.sentences}
    facts = [
        fact.model_copy(update={"surface_refs": [sentences[fact.fact_id].text]})
        for fact in world.facts
    ]
    sessions: list[Session] = []
    for session in world.sessions:
        turns: list[SessionTurn] = []
        for turn in session.turns:
            if len(turn.fact_ids) == 1 and turn.fact_ids[0] in sentences:
                sentence = sentences[turn.fact_ids[0]]
                turns.append(
                    turn.model_copy(update={"speaker": sentence.speaker, "text": sentence.text})
                )
            else:
                turns.append(turn)
        sessions.append(session.model_copy(update={"turns": turns}))
    return world.model_copy(update={"facts": facts, "sessions": sessions})


def _apply_questions(world: WorldSpec, rendered: QuestionRender) -> WorldSpec:
    questions = {question.probe_id: question.question for question in rendered.questions}
    probes = [
        probe.model_copy(update={"question": questions[probe.probe_id]}) for probe in world.probes
    ]
    return world.model_copy(update={"probes": probes})
