from __future__ import annotations

import logging
from collections.abc import Sequence

from modex_agent.core.llm_struct import LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.memory.hooks import LlmUsage


class _ResponseProvider(CallbackStreamProvider):
    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        super().__init__()
        self._responses = iter(responses)

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
        del messages, model, temperature, max_output_tokens, tools, kwargs
        return next(self._responses)

    def get_default_model(self) -> str:
        return "fallback-model"


async def test_missing_usage_fields_count_call_with_zero_buckets() -> None:
    # Given
    from modex_agent.agents.summarizer.scoped_file_agent import UsageCollectingProvider

    collector = UsageCollectingProvider(_ResponseProvider([LLMResponse(content="done")]))

    # When
    await collector.chat([])

    # Then
    assert collector.operation_usage() == LlmUsage(
        model="fallback-model",
        calls=1,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )


async def test_multiple_models_warn_and_return_first_model_usage(caplog) -> None:
    # Given
    from modex_agent.agents.summarizer.scoped_file_agent import UsageCollectingProvider

    collector = UsageCollectingProvider(
        _ResponseProvider(
            [
                LLMResponse(content="first", usage={"prompt_tokens": 2}),
                LLMResponse(content="second", usage={"completion_tokens": 3}),
            ]
        )
    )

    # When
    with caplog.at_level(logging.WARNING):
        await collector.chat([], model="model-a")
        await collector.chat([], model="model-b")
        usage = collector.operation_usage()

    # Then
    assert usage == LlmUsage(
        model="model-a",
        calls=1,
        input_tokens=2,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    assert "multiple models" in caplog.text.lower()
