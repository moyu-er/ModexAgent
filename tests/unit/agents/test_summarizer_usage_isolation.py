from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import anyio

from modex_agent.agents.summarizer.session_compactor import SessionCompactorAgent
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.types import LLMResponse, MessageRole
from modex_agent.memory.hooks import LlmUsage

_MODEL: Final = "isolated-model"


class _InterleavingProvider(CallbackStreamProvider):
    def __init__(self) -> None:
        super().__init__()
        operation_a = anyio.Event()
        operation_b = anyio.Event()
        self._gates = {
            "operation-a": (operation_a, operation_b),
            "operation-b": (operation_b, operation_a),
        }
        self._usage = {
            "operation-a": {
                "prompt_tokens": 24,
                "completion_tokens": 12,
                "cache_read_input_tokens": 13,
                "cache_creation_input_tokens": 14,
            },
            "operation-b": {
                "prompt_tokens": 44,
                "completion_tokens": 22,
                "cache_read_input_tokens": 23,
                "cache_creation_input_tokens": 24,
            },
        }

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
        del messages, model, temperature, max_output_tokens, tools
        session_id = str(kwargs["prompt_cache_key"])
        own_gate, other_gate = self._gates[session_id]
        own_gate.set()
        await other_gate.wait()
        return LLMResponse(content=session_id, usage=self._usage[session_id])

    def get_default_model(self) -> str:
        return _MODEL


class _SequentialProvider(CallbackStreamProvider):
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
        return _MODEL


async def test_concurrent_compactions_keep_usage_operation_local() -> None:
    # Given
    compactor = SessionCompactorAgent(_InterleavingProvider())
    usage_a: LlmUsage | None = None
    usage_b: LlmUsage | None = None

    async def compact_a() -> None:
        nonlocal usage_a
        outcome = await compactor.compact(
            [{"role": MessageRole.USER, "content": "A"}], session_id="operation-a"
        )
        usage_a = outcome.usage

    async def compact_b() -> None:
        nonlocal usage_b
        outcome = await compactor.compact(
            [{"role": MessageRole.USER, "content": "B"}], session_id="operation-b"
        )
        usage_b = outcome.usage

    # When
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(compact_a)
        task_group.start_soon(compact_b)

    # Then
    assert usage_a == LlmUsage(
        model=_MODEL,
        calls=1,
        input_tokens=11,
        output_tokens=12,
        cache_read_tokens=13,
        cache_write_tokens=14,
    )
    assert usage_b == LlmUsage(
        model=_MODEL,
        calls=1,
        input_tokens=21,
        output_tokens=22,
        cache_read_tokens=23,
        cache_write_tokens=24,
    )


async def test_compact_uses_fresh_collector_for_each_operation() -> None:
    # Given
    compactor = SessionCompactorAgent(
        _SequentialProvider(
            [
                LLMResponse(content="first", usage={"prompt_tokens": 5}),
                LLMResponse(content="second", usage={"prompt_tokens": 7}),
            ]
        )
    )

    # When
    first = await compactor.compact(
        [{"role": MessageRole.USER, "content": "first"}], session_id="first"
    )
    second = await compactor.compact(
        [{"role": MessageRole.USER, "content": "second"}], session_id="second"
    )

    # Then
    assert first.usage == LlmUsage(
        model=_MODEL,
        calls=1,
        input_tokens=5,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    assert second.usage == LlmUsage(
        model=_MODEL,
        calls=1,
        input_tokens=7,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
