"""Tests for EventAssembler (core/stream_events).

Locks the terminal-event invariant (EOF without a terminal event, feed
after terminal, result() idempotence), the replay-priority rules, the
partial-content splice order, and the sync/async delta callbacks.
"""

import time
from collections.abc import Sequence
from datetime import datetime

import pytest

from modex_agent.core.llm_struct import FinishReason, LLMErrorInfo, LLMErrorKind, TokenUsage
from modex_agent.core.stream_events import (
    EventAssembler,
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    ReplayFields,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)


async def feed_all(assembler: EventAssembler, events: Sequence[LLMStreamEvent]) -> None:
    """Feed a full event sequence (mirrors both consumer loops: T13 fold, T20 inline)."""
    for event in events:
        await assembler.feed(event)


class TestSequenceAssembly:
    @pytest.mark.parametrize(
        ("events", "expected_content", "expected_reasoning", "expected_finish"),
        [
            pytest.param(
                [
                    TextDelta(text="Hello "),
                    TextDelta(text="world"),
                    Finish(finish_reason=FinishReason.STOP),
                ],
                "Hello world",
                None,
                FinishReason.STOP,
                id="text-only",
            ),
            pytest.param(
                [
                    ReasoningDelta(text="think "),
                    ReasoningDelta(text="hard"),
                    Finish(finish_reason=FinishReason.STOP),
                ],
                None,
                "think hard",
                FinishReason.STOP,
                id="reasoning-only",
            ),
            pytest.param(
                [
                    ToolCallComplete(
                        call_id="call_1", tool_name="get_weather", arguments={"city": "Paris"}
                    ),
                    Finish(finish_reason=FinishReason.TOOL_CALLS),
                ],
                None,
                None,
                FinishReason.TOOL_CALLS,
                id="tools-only",
            ),
            pytest.param(
                [Finish(finish_reason=FinishReason.LENGTH)],
                None,
                None,
                FinishReason.LENGTH,
                id="finish-only",
            ),
        ],
    )
    async def test_sequence_assembles_core_fields(
        self,
        events: Sequence[LLMStreamEvent],
        expected_content: str | None,
        expected_reasoning: str | None,
        expected_finish: FinishReason,
    ) -> None:
        assembler = EventAssembler()

        await feed_all(assembler, events)

        response = assembler.result()
        assert response.content == expected_content
        assert response.reasoning_content == expected_reasoning
        assert response.finish_reason == expected_finish
        assert response.error is None
        assert response.error_info is None

    async def test_full_sequence_assembles_every_field(self) -> None:
        usage = TokenUsage(input_tokens=3, cache_read_input_tokens=7, output_tokens=5)
        replay = ReplayFields(
            reasoning_content="final reasoning",
            reasoning_signature="sig-1",
            reasoning_item_id="item_9",
            reasoning_encrypted_content="enc-blob",
        )
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [
                ReasoningDelta(text="acc "),
                TextDelta(text="Hello"),
                ToolCallComplete(
                    call_id="call_1", tool_name="get_weather", arguments={"city": "Paris"}
                ),
                UsageSnapshot(usage=usage),
                Finish(finish_reason=FinishReason.TOOL_CALLS, replay=replay),
            ],
        )

        response = assembler.result()
        assert response.content == "Hello"
        # Replay carries the engine's final reasoning value.
        assert response.reasoning_content == "final reasoning"
        assert response.reasoning_signature == "sig-1"
        assert response.reasoning_item_id == "item_9"
        assert response.reasoning_encrypted_content == "enc-blob"
        assert response.finish_reason == FinishReason.TOOL_CALLS
        assert response.usage == usage
        assert response.usage.total_tokens == 15
        assert [(tc.call_id, tc.tool_name, tc.arguments) for tc in response.tool_calls] == [
            ("call_1", "get_weather", {"city": "Paris"})
        ]

    async def test_multiple_tool_calls_keep_arrival_order(self) -> None:
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [
                ToolCallComplete(call_id="call_1", tool_name="a", arguments={}),
                ToolCallComplete(call_id="call_2", tool_name="b", arguments={"x": 1}),
                Finish(finish_reason=FinishReason.TOOL_CALLS),
            ],
        )

        assert [tc.call_id for tc in assembler.result().tool_calls] == ["call_1", "call_2"]


class TestEofWithoutTerminal:
    async def test_eof_after_deltas_synthesizes_timeout_error(self) -> None:
        assembler = EventAssembler()

        await feed_all(assembler, [TextDelta(text="partial "), TextDelta(text="content")])

        response = assembler.result()
        assert response.finish_reason == FinishReason.ERROR
        assert response.content == "partial content"
        assert response.error == "stream ended without terminal event"
        assert response.error_info is not None
        assert response.error_info.kind == LLMErrorKind.TIMEOUT
        assert response.error_info.should_retry is True
        assert response.error_info.provider is None

    async def test_eof_with_no_events_keeps_empty_accumulated_content(self) -> None:
        response = EventAssembler().result()

        assert response.finish_reason == FinishReason.ERROR
        assert response.content == ""
        assert response.error_info is not None
        assert response.error_info.kind == LLMErrorKind.TIMEOUT

    async def test_eof_error_response_has_no_tool_calls_or_usage(self) -> None:
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [
                ToolCallComplete(call_id="call_1", tool_name="a", arguments={}),
                UsageSnapshot(usage=TokenUsage(input_tokens=1)),
            ],
        )

        response = assembler.result()
        # Error responses keep the legacy build_timeout_response shape:
        # content + error + error_info only.
        assert response.tool_calls == []
        assert response.usage == TokenUsage()
        assert response.completion_start_time is None


class TestStreamFailure:
    async def test_failure_splices_partial_in_front_of_accumulated(self) -> None:
        error_info = LLMErrorInfo(
            kind=LLMErrorKind.SERVER, message="boom", provider="anthropic", should_retry=True
        )
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [TextDelta(text="A"), StreamFailure(error_info=error_info, partial_content="B")],
        )

        response = assembler.result()
        assert response.content == "BA"
        assert response.finish_reason == FinishReason.ERROR
        assert response.error == "boom"
        assert response.error_info == error_info

    async def test_failure_without_partial_keeps_accumulated_content(self) -> None:
        error_info = LLMErrorInfo(kind=LLMErrorKind.CONNECTION, message="reset", should_retry=True)
        assembler = EventAssembler()

        await feed_all(assembler, [TextDelta(text="kept"), StreamFailure(error_info=error_info)])

        response = assembler.result()
        assert response.content == "kept"
        assert response.error == "reset"
        assert response.error_info is not None
        assert response.error_info.kind == LLMErrorKind.CONNECTION

    async def test_failure_as_only_event_yields_partial_content(self) -> None:
        error_info = LLMErrorInfo(kind=LLMErrorKind.AUTH, message="bad key", should_retry=False)
        assembler = EventAssembler()

        await feed_all(assembler, [StreamFailure(error_info=error_info, partial_content="x")])

        response = assembler.result()
        assert response.content == "x"
        assert response.finish_reason == FinishReason.ERROR
        assert response.error_info is not None
        assert response.error_info.should_retry is False


class TestTerminalInvariant:
    async def test_feed_after_finish_raises(self) -> None:
        assembler = EventAssembler()
        await feed_all(assembler, [Finish(finish_reason=FinishReason.STOP)])

        with pytest.raises(RuntimeError):
            await assembler.feed(TextDelta(text="late"))

    async def test_feed_after_stream_failure_raises(self) -> None:
        assembler = EventAssembler()
        await feed_all(
            assembler,
            [StreamFailure(error_info=LLMErrorInfo(kind=LLMErrorKind.UNKNOWN, message="x"))],
        )

        with pytest.raises(RuntimeError):
            await assembler.feed(Finish(finish_reason=FinishReason.STOP))

    async def test_feed_after_result_raises_even_without_terminal_event(self) -> None:
        assembler = EventAssembler()
        await assembler.feed(TextDelta(text="x"))
        assembler.result()  # EOF synthesis closes the assembler too.

        with pytest.raises(RuntimeError):
            await assembler.feed(TextDelta(text="late"))

    async def test_result_is_idempotent_after_finish(self) -> None:
        assembler = EventAssembler()
        await feed_all(
            assembler,
            [
                TextDelta(text="hi"),
                ReasoningDelta(text="thought"),
                Finish(
                    finish_reason=FinishReason.STOP,
                    replay=ReplayFields(reasoning_signature="s"),
                ),
            ],
        )

        assert assembler.result() == assembler.result()

    async def test_result_is_idempotent_after_eof(self) -> None:
        assembler = EventAssembler()
        await assembler.feed(TextDelta(text="x"))

        assert assembler.result() == assembler.result()

    async def test_result_is_idempotent_after_failure(self) -> None:
        assembler = EventAssembler()
        await feed_all(
            assembler,
            [
                TextDelta(text="a"),
                StreamFailure(error_info=LLMErrorInfo(kind=LLMErrorKind.SERVER, message="m")),
            ],
        )

        assert assembler.result() == assembler.result()


class TestDeltaCallbacks:
    async def test_sync_callbacks_receive_deltas_in_order(self) -> None:
        content_deltas: list[str] = []
        reasoning_deltas: list[str] = []
        assembler = EventAssembler(
            on_content_delta=content_deltas.append,
            on_reasoning_delta=reasoning_deltas.append,
        )

        await feed_all(
            assembler,
            [
                TextDelta(text="a"),
                ReasoningDelta(text="r"),
                TextDelta(text="b"),
                Finish(finish_reason=FinishReason.STOP),
            ],
        )

        assert content_deltas == ["a", "b"]
        assert reasoning_deltas == ["r"]

    async def test_async_callbacks_are_awaited(self) -> None:
        received: list[str] = []

        async def on_delta(text: str) -> None:
            received.append(text)

        assembler = EventAssembler(on_content_delta=on_delta, on_reasoning_delta=on_delta)

        await feed_all(
            assembler,
            [
                TextDelta(text="x"),
                ReasoningDelta(text="y"),
                Finish(finish_reason=FinishReason.STOP),
            ],
        )

        assert received == ["x", "y"]

    async def test_empty_text_deltas_fire_no_callback(self) -> None:
        calls: list[str] = []
        assembler = EventAssembler(on_content_delta=calls.append, on_reasoning_delta=calls.append)

        await feed_all(
            assembler,
            [TextDelta(text=""), ReasoningDelta(text=""), Finish(finish_reason=FinishReason.STOP)],
        )

        assert calls == []
        response = assembler.result()
        assert response.content is None
        assert response.reasoning_content is None

    async def test_none_callbacks_are_tolerated(self) -> None:
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [
                TextDelta(text="x"),
                ReasoningDelta(text="r"),
                Finish(finish_reason=FinishReason.STOP),
            ],
        )

        response = assembler.result()
        assert response.content == "x"
        assert response.reasoning_content == "r"

    async def test_callbacks_do_not_fire_for_terminal_events(self) -> None:
        content_calls: list[str] = []
        reasoning_calls: list[str] = []
        assembler = EventAssembler(
            on_content_delta=content_calls.append,
            on_reasoning_delta=reasoning_calls.append,
        )
        error_info = LLMErrorInfo(kind=LLMErrorKind.SERVER, message="m")

        await feed_all(
            assembler,
            [
                UsageSnapshot(usage=TokenUsage(input_tokens=1)),
                StreamFailure(error_info=error_info, partial_content="p"),
            ],
        )

        assert content_calls == []
        assert reasoning_calls == []


class TestReplayPriority:
    async def test_replay_reasoning_content_wins_over_accumulated(self) -> None:
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [
                ReasoningDelta(text="acc"),
                Finish(
                    finish_reason=FinishReason.STOP,
                    replay=ReplayFields(reasoning_content="final"),
                ),
            ],
        )

        assert assembler.result().reasoning_content == "final"

    async def test_replay_without_reasoning_falls_back_to_accumulated(self) -> None:
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [
                ReasoningDelta(text="acc"),
                Finish(
                    finish_reason=FinishReason.STOP,
                    replay=ReplayFields(reasoning_signature="sig"),
                ),
            ],
        )

        response = assembler.result()
        assert response.reasoning_content == "acc"
        assert response.reasoning_signature == "sig"
        assert response.reasoning_item_id is None
        assert response.reasoning_encrypted_content is None

    async def test_no_replay_leaves_replay_fields_none(self) -> None:
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [ReasoningDelta(text="r"), Finish(finish_reason=FinishReason.STOP)],
        )

        response = assembler.result()
        assert response.reasoning_content == "r"
        assert response.reasoning_signature is None
        assert response.reasoning_item_id is None
        assert response.reasoning_encrypted_content is None


class TestUsageSnapshots:
    async def test_last_usage_snapshot_wins(self) -> None:
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [
                UsageSnapshot(usage=TokenUsage(input_tokens=1)),
                UsageSnapshot(usage=TokenUsage(input_tokens=2, output_tokens=3)),
                Finish(finish_reason=FinishReason.STOP),
            ],
        )

        assert assembler.result().usage == TokenUsage(input_tokens=2, output_tokens=3)

    async def test_no_usage_snapshot_yields_zero_usage(self) -> None:
        assembler = EventAssembler()

        await feed_all(assembler, [Finish(finish_reason=FinishReason.STOP)])

        assert assembler.result().usage == TokenUsage()


class TestCompletionStartTime:
    async def test_stamped_from_first_event_as_utc_isoformat(self) -> None:
        before = time.time()
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [TextDelta(text="x"), Finish(finish_reason=FinishReason.STOP)],
        )

        response = assembler.result()
        after = time.time()
        assert response.completion_start_time is not None
        stamped = datetime.fromisoformat(response.completion_start_time)
        assert stamped.tzinfo is not None
        # ISO microseconds truncate the float timestamp, so allow a 1µs
        # rounding tolerance on both bounds.
        assert before - 1e-6 <= stamped.timestamp() <= after + 1e-6

    async def test_first_event_of_any_kind_stamps_the_time(self) -> None:
        assembler = EventAssembler()

        await feed_all(
            assembler,
            [
                ToolCallComplete(call_id="call_1", tool_name="t", arguments={}),
                Finish(finish_reason=FinishReason.TOOL_CALLS),
            ],
        )

        assert assembler.result().completion_start_time is not None
