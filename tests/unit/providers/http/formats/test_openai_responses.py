"""Tests for modex_agent.providers.http.formats.openai_responses.

Canned Responses SSE event streams (SseFrame sequences, zero network) drive
the events() translator; build_body() is asserted independently on canonical
LLMRequest inputs. Scenarios mirror the task list: text stream, reasoning
summary replay, interleaved tool calls, zero-argument calls, authoritative
done-value override, failed/incomplete terminals, wire-shape lowering, and
unknown-event tolerance.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from modex_agent.core.constants import FinishReason, ReasoningEffort
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import LLMErrorKind
from modex_agent.core.message import ChatMessage, ImageUrl, ImageUrlPart, TextPart
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    ReplayFields,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)
from modex_agent.core.types import MessageRole, TokenUsage, ToolCall
from modex_agent.providers.http.formats.openai_responses import OpenAIResponsesProtocol
from modex_agent.providers.http.protocol import ProtocolConfig
from modex_agent.providers.http.sse import SseFrame

_ENGINE = OpenAIResponsesProtocol()
_LOGGER_NAME = "modex_agent.providers.http.formats.openai_responses"


def _frame(event: str | None, payload: dict[str, Any] | str) -> SseFrame:
    """One SSE frame; dict payloads get the event name mirrored into ``type``."""
    if isinstance(payload, str):
        return SseFrame(event=event, data=payload)
    data = {**payload, "type": event} if event is not None else dict(payload)
    return SseFrame(event=event, data=json.dumps(data))


def _frames(*frames: SseFrame) -> AsyncIterator[SseFrame]:
    """In-memory frame stream — zero network."""

    async def _gen() -> AsyncIterator[SseFrame]:
        for frame in frames:
            yield frame

    return _gen()


async def _run(*frames: SseFrame) -> list[LLMStreamEvent]:
    """Drive the engine's events() translator over canned frames."""
    return [event async for event in _ENGINE.events(_frames(*frames))]


def _cfg(
    store: bool = True,
    effort: ReasoningEffort = ReasoningEffort.NONE,
    extra_body: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
) -> ProtocolConfig:
    return ProtocolConfig(
        store=store,
        reasoning_effort=effort,
        extra_body=extra_body,
        max_output_tokens=max_output_tokens,
    )


def _completions(events: list[LLMStreamEvent]) -> list[ToolCallComplete]:
    return [event for event in events if isinstance(event, ToolCallComplete)]


class TestEventStreamTranslation:
    async def test_text_stream_yields_deltas_usage_then_stop_finish(self) -> None:
        events = await _run(
            _frame("response.created", {"response": {"id": "resp_1"}}),
            _frame("response.in_progress", {"response": {"id": "resp_1"}}),
            _frame(
                "response.output_item.added",
                {
                    "output_index": 0,
                    "item": {"type": "message", "role": "assistant", "id": "msg_1", "content": []},
                },
            ),
            _frame("response.content_part.added", {"item_id": "msg_1"}),
            _frame("response.output_text.delta", {"item_id": "msg_1", "delta": "Hello"}),
            _frame("response.output_text.delta", {"item_id": "msg_1", "delta": " world"}),
            _frame("response.output_text.done", {"item_id": "msg_1", "text": "Hello world"}),
            _frame("response.content_part.done", {"item_id": "msg_1"}),
            _frame(
                "response.output_item.done",
                {
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": "msg_1",
                        "content": [{"type": "output_text", "text": "Hello world"}],
                    }
                },
            ),
            _frame(
                "response.completed",
                {
                    "response": {
                        "id": "resp_1",
                        "usage": {
                            "input_tokens": 10,
                            "input_tokens_details": {"cached_tokens": 4},
                            "output_tokens": 7,
                            "output_tokens_details": {"reasoning_tokens": 2},
                        },
                    }
                },
            ),
        )
        assert events == [
            TextDelta(text="Hello"),
            TextDelta(text=" world"),
            UsageSnapshot(
                usage=TokenUsage(
                    input_tokens=10,
                    cache_read_input_tokens=4,
                    output_tokens=7,
                    reasoning_tokens=2,
                )
            ),
            Finish(finish_reason=FinishReason.STOP, replay=None),
        ]

    async def test_reasoning_summary_stream_replays_item_id_and_encrypted_content(
        self,
    ) -> None:
        events = await _run(
            _frame(
                "response.output_item.added",
                {"item": {"type": "reasoning", "id": "rs_1", "summary": []}},
            ),
            _frame(
                "response.reasoning_summary_text.delta",
                {"item_id": "rs_1", "summary_index": 0, "delta": "Think "},
            ),
            _frame(
                # Unknown reasoning delta family: same ReasoningDelta class.
                "response.reasoning_text.delta",
                {"item_id": "rs_1", "delta": "hard"},
            ),
            _frame(
                "response.output_item.done",
                {
                    "item": {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [],
                        "encrypted_content": "enc-abc",
                    }
                },
            ),
            _frame(
                "response.completed",
                {"response": {"id": "resp_2", "usage": {"input_tokens": 5, "output_tokens": 3}}},
            ),
        )
        assert events == [
            ReasoningDelta(text="Think "),
            ReasoningDelta(text="hard"),
            UsageSnapshot(usage=TokenUsage(input_tokens=5, output_tokens=3)),
            Finish(
                finish_reason=FinishReason.STOP,
                replay=ReplayFields(
                    reasoning_item_id="rs_1", reasoning_encrypted_content="enc-abc"
                ),
            ),
        ]

    async def test_interleaved_tool_calls_complete_with_call_id_not_item_id(self) -> None:
        events = await _run(
            _frame(
                "response.output_item.added",
                {
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_abc",
                        "name": "get_weather",
                        "arguments": "",
                    },
                },
            ),
            _frame(
                "response.output_item.added",
                {
                    "output_index": 1,
                    "item": {
                        "type": "function_call",
                        "id": "fc_2",
                        "call_id": "call_def",
                        "name": "get_time",
                        "arguments": "",
                    },
                },
            ),
            _frame(
                "response.function_call_arguments.delta",
                {"item_id": "fc_1", "delta": '{"ci'},
            ),
            _frame(
                "response.function_call_arguments.delta",
                {"item_id": "fc_2", "delta": '{"zone"'},
            ),
            _frame(
                "response.function_call_arguments.delta",
                {"item_id": "fc_1", "delta": 'ty": "Beijing"}'},
            ),
            _frame(
                "response.output_item.done",
                {
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_abc",
                        "name": "get_weather",
                        "arguments": '{"city": "Beijing"}',
                    }
                },
            ),
            _frame(
                "response.output_item.done",
                {
                    "item": {
                        "type": "function_call",
                        "id": "fc_2",
                        "call_id": "call_def",
                        "name": "get_time",
                        "arguments": '{"zone": "UTC"}',
                    }
                },
            ),
            _frame(
                "response.completed",
                {"response": {"id": "resp_3", "usage": {"input_tokens": 9, "output_tokens": 6}}},
            ),
        )
        completions = _completions(events)
        assert completions == [
            ToolCallComplete(
                call_id="call_abc", tool_name="get_weather", arguments={"city": "Beijing"}
            ),
            ToolCallComplete(call_id="call_def", tool_name="get_time", arguments={"zone": "UTC"}),
        ]
        # Red line: ToolCallComplete.call_id is the call_id (call_...), never
        # the stream key (fc_...) — the next turn's function_call_output pairs
        # on this value.
        assert all(completion.call_id.startswith("call_") for completion in completions)
        finish = events[-1]
        assert isinstance(finish, Finish)
        assert finish.finish_reason is FinishReason.TOOL_CALLS

    async def test_zero_argument_call_finishes_with_empty_arguments(self) -> None:
        events = await _run(
            _frame(
                "response.output_item.added",
                {
                    "item": {
                        "type": "function_call",
                        "id": "fc_0",
                        "call_id": "call_x",
                        "name": "ping",
                        "arguments": "",
                    }
                },
            ),
            _frame(
                "response.output_item.done",
                {
                    "item": {
                        "type": "function_call",
                        "id": "fc_0",
                        "call_id": "call_x",
                        "name": "ping",
                        "arguments": "{}",
                    }
                },
            ),
            _frame("response.completed", {"response": {"id": "resp_4"}}),
        )
        assert _completions(events) == [
            ToolCallComplete(call_id="call_x", tool_name="ping", arguments={})
        ]
        # completed without usage: no UsageSnapshot, but Finish is still emitted.
        assert not any(isinstance(event, UsageSnapshot) for event in events)
        finish = events[-1]
        assert isinstance(finish, Finish)
        assert finish.finish_reason is FinishReason.TOOL_CALLS

    async def test_done_final_arguments_override_partial_accumulation(self) -> None:
        events = await _run(
            _frame(
                "response.output_item.added",
                {
                    "item": {
                        "type": "function_call",
                        "id": "fc_9",
                        "call_id": "call_half",
                        "name": "search",
                        "arguments": "",
                    }
                },
            ),
            # Deltas cut mid-stream...
            _frame(
                "response.function_call_arguments.delta",
                {"item_id": "fc_9", "delta": '{"que'},
            ),
            # ...but the done event carries the authoritative full value.
            _frame(
                "response.output_item.done",
                {
                    "item": {
                        "type": "function_call",
                        "id": "fc_9",
                        "call_id": "call_half",
                        "name": "search",
                        "arguments": '{"query": "full"}',
                    }
                },
            ),
            _frame("response.completed", {"response": {"id": "resp_5"}}),
        )
        assert _completions(events) == [
            ToolCallComplete(call_id="call_half", tool_name="search", arguments={"query": "full"})
        ]

    async def test_response_failed_yields_stream_failure(self) -> None:
        events = await _run(
            _frame("response.output_text.delta", {"item_id": "msg_1", "delta": "partial"}),
            _frame(
                "response.failed",
                {
                    "response": {
                        "id": "resp_6",
                        "error": {"code": "rate_limit_exceeded", "message": "Slow down"},
                    }
                },
            ),
        )
        assert events[0] == TextDelta(text="partial")
        failure = events[1]
        assert isinstance(failure, StreamFailure)
        assert failure.error_info.message == "rate_limit_exceeded: Slow down"
        assert failure.error_info.kind is LLMErrorKind.SERVER
        assert failure.error_info.should_retry is True

    async def test_response_failed_context_overflow_is_not_retried(self) -> None:
        events = await _run(
            _frame(
                "response.failed",
                {
                    "response": {
                        "error": {
                            "code": "context_length_exceeded",
                            "message": "This model's maximum context length is 4096 tokens",
                        }
                    }
                },
            ),
        )
        failure = events[-1]
        assert isinstance(failure, StreamFailure)
        assert failure.error_info.kind is LLMErrorKind.INVALID_REQUEST
        assert failure.error_info.should_retry is False

    async def test_response_incomplete_finishes_with_length_and_discards_pending(
        self,
    ) -> None:
        events = await _run(
            _frame(
                "response.output_item.added",
                {
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_trunc",
                        "name": "search",
                        "arguments": "",
                    }
                },
            ),
            _frame(
                "response.function_call_arguments.delta",
                {"item_id": "fc_1", "delta": '{"qu'},
            ),
            _frame(
                "response.incomplete",
                {
                    "response": {
                        "id": "resp_7",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "usage": {"input_tokens": 8, "output_tokens": 4},
                    }
                },
            ),
        )
        # LENGTH discards the truncated pending accumulation.
        assert not _completions(events)
        assert events == [
            UsageSnapshot(usage=TokenUsage(input_tokens=8, output_tokens=4)),
            Finish(finish_reason=FinishReason.LENGTH, replay=None),
        ]

    async def test_unknown_events_are_skipped_and_gateway_frames_dispatch_on_type(
        self,
    ) -> None:
        events = await _run(
            _frame("response.web_search_call.in_progress", {"item_id": "ws_1"}),
            _frame("response.web_search_call.completed", {"item_id": "ws_1"}),
            # No event line (gateway compatibility): dispatch on data.type.
            _frame(
                None,
                {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "via type"},
            ),
            _frame(None, {"type": "response.completed", "response": {"id": "resp_8"}}),
        )
        assert events == [
            TextDelta(text="via type"),
            Finish(finish_reason=FinishReason.STOP, replay=None),
        ]

    async def test_orphan_done_item_is_tolerated_without_crashing(self) -> None:
        """Gateway reordering: done arrives with no preceding added.

        The done item carries full identity (id/call_id/name/arguments), so
        the call is recovered instead of crashing or dropping it.
        """
        events = await _run(
            _frame(
                "response.output_item.done",
                {
                    "item": {
                        "type": "function_call",
                        "id": "fc_orphan",
                        "call_id": "call_orphan",
                        "name": "search",
                        "arguments": '{"q": 1}',
                    }
                },
            ),
            _frame("response.completed", {"response": {"id": "resp_9"}}),
        )
        assert _completions(events) == [
            ToolCallComplete(call_id="call_orphan", tool_name="search", arguments={"q": 1})
        ]

    async def test_malformed_json_frame_yields_stream_failure(self) -> None:
        events = await _run(
            _frame("response.output_text.delta", {"item_id": "m", "delta": "ok"}),
            _frame("response.output_text.delta", "not json"),
        )
        assert events[0] == TextDelta(text="ok")
        failure = events[1]
        assert isinstance(failure, StreamFailure)
        assert failure.error_info.kind is LLMErrorKind.INVALID_REQUEST
        assert "malformed JSON frame" in failure.error_info.message

    async def test_engine_instance_is_stateless_across_streams(self) -> None:
        await _run(
            _frame(
                "response.output_item.added",
                {
                    "item": {
                        "type": "function_call",
                        "id": "fc_a",
                        "call_id": "call_a",
                        "name": "search",
                        "arguments": "",
                    }
                },
            ),
            _frame("response.completed", {"response": {"id": "resp_a"}}),
        )
        # A second stream through the SAME instance starts from clean state.
        events = await _run(_frame("response.completed", {"response": {"id": "resp_b"}}))
        assert events == [Finish(finish_reason=FinishReason.STOP, replay=None)]


class TestBuildBody:
    def test_merges_system_messages_into_instructions(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content="Be terse."),
                ChatMessage(role=MessageRole.USER, content="hi"),
                ChatMessage(role=MessageRole.SYSTEM, content="Second rule."),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())
        assert body["instructions"] == "Be terse.\n\nSecond rule."
        assert body["input"] == [{"role": "user", "content": "hi"}]
        # V1: stop and prompt_cache_key have no Responses wire field (PRD §3).
        assert "stop" not in body
        assert "prompt_cache_key" not in body

    def test_no_system_messages_omits_instructions_key(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )
        body = _ENGINE.build_body(request, _cfg())
        assert "instructions" not in body

    def test_flattens_nested_tools_and_passes_sampling_parameters(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[ChatMessage(role=MessageRole.USER, content="weather?")],
            tools=(
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                },
            ),
            temperature=0.2,
            top_p=0.9,
            max_output_tokens=1024,
        )
        body = _ENGINE.build_body(request, _cfg())
        assert body["tools"] == [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            }
        ]
        assert body["temperature"] == 0.2
        assert body["top_p"] == 0.9
        assert body["max_output_tokens"] == 1024
        assert body["stream"] is True
        assert body["store"] is True

    def test_reasoning_effort_from_request_then_config(self) -> None:
        messages = [ChatMessage(role=MessageRole.USER, content="hi")]
        body = _ENGINE.build_body(LLMRequest(model="gpt-5", messages=messages), _cfg())
        assert "reasoning" not in body

        body = _ENGINE.build_body(
            LLMRequest(model="gpt-5", messages=messages), _cfg(effort=ReasoningEffort.HIGH)
        )
        assert body["reasoning"] == {"effort": "high"}

        body = _ENGINE.build_body(
            LLMRequest(
                model="gpt-5",
                messages=messages,
                reasoning_effort=ReasoningEffort.LOW,
            ),
            _cfg(effort=ReasoningEffort.HIGH),
        )
        assert body["reasoning"] == {"effort": "low"}

    def test_none_sampling_parameters_are_omitted(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )
        body = _ENGINE.build_body(request, _cfg())
        assert "temperature" not in body
        assert "top_p" not in body
        assert "max_output_tokens" not in body
        assert "tools" not in body

    def test_store_true_replays_item_reference(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(role=MessageRole.USER, content="hi"),
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content="answer",
                    reasoning_item_id="rs_1",
                    reasoning_encrypted_content="enc-1",
                    tool_calls=[
                        ToolCall(tool_name="search", arguments={"q": "x"}, call_id="call_1")
                    ],
                ),
                ChatMessage(role=MessageRole.TOOL, content="result text", tool_call_id="call_1"),
            ],
        )
        body = _ENGINE.build_body(request, _cfg(store=True))
        assert body["input"] == [
            {"role": "user", "content": "hi"},
            {"type": "item_reference", "id": "rs_1"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer"}],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"q": "x"}',
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "result text"},
        ]
        assert "include" not in body
        assert body["store"] is True

    def test_store_false_replays_full_reasoning_item_with_include(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content="answer",
                    reasoning_item_id="rs_1",
                    reasoning_encrypted_content="enc-1",
                ),
            ],
        )
        body = _ENGINE.build_body(request, _cfg(store=False))
        assert body["input"] == [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
                "encrypted_content": "enc-1",
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer"}],
            },
        ]
        assert body["include"] == ["reasoning.encrypted_content"]
        assert body["store"] is False

    def test_store_false_without_encrypted_content_drops_reasoning_item(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content="answer",
                    reasoning_item_id="rs_2",
                ),
            ],
        )
        body = _ENGINE.build_body(request, _cfg(store=False))
        assert body["input"] == [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer"}],
            },
        ]

    def test_assistant_tool_calls_use_canonical_call_id_and_json_arguments(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        ToolCall(tool_name="a", arguments={"x": 1}, call_id="call_a"),
                        ToolCall(tool_name="b", arguments={}, call_id="call_b"),
                    ],
                ),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())
        assert body["input"] == [
            {"type": "function_call", "call_id": "call_a", "name": "a", "arguments": '{"x": 1}'},
            {"type": "function_call", "call_id": "call_b", "name": "b", "arguments": "{}"},
        ]

    def test_empty_tool_output_becomes_placeholder(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(role=MessageRole.TOOL, content="", tool_call_id="call_1"),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())
        assert body["input"] == [
            {"type": "function_call_output", "call_id": "call_1", "output": "(no output)"}
        ]

    def test_lowers_user_content_parts(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.USER,
                    content=[
                        TextPart(text="look:"),
                        ImageUrlPart(image_url=ImageUrl(url="https://example.com/cat.png")),
                    ],
                ),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())
        assert body["input"] == [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "look:"},
                    {"type": "input_image", "image_url": "https://example.com/cat.png"},
                ],
            }
        ]

    def test_unresolved_media_refs_are_skipped_with_error_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.SYSTEM,
                    content=[
                        TextPart(text="system"),
                        ImageUrlPart(image_url=ImageUrl(url="media://system-image")),
                    ],
                ),
                ChatMessage(
                    role=MessageRole.USER,
                    content=[
                        TextPart(text="user"),
                        ImageUrlPart(image_url=ImageUrl(url="media://user-image")),
                    ],
                ),
            ],
        )

        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            body = _ENGINE.build_body(request, _cfg())

        assert "media://" not in json.dumps(body)
        assert body["instructions"] == "system"
        assert body["input"] == [
            {"role": "user", "content": [{"type": "input_text", "text": "user"}]}
        ]
        assert [record.getMessage() for record in caplog.records] == [
            "openai_responses engine: unresolved media:// reference reached the wire layer, "
            "part skipped: system-image",
            "openai_responses engine: unresolved media:// reference reached the wire layer, "
            "part skipped: user-image",
        ]

    def test_data_url_image_part_passes_through(self) -> None:
        data_url = "data:image/png;base64,aGVsbG8="
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.USER,
                    content=[ImageUrlPart(image_url=ImageUrl(url=data_url))],
                )
            ],
        )

        body = _ENGINE.build_body(request, _cfg())

        assert body["input"] == [
            {
                "role": "user",
                "content": [{"type": "input_image", "image_url": data_url}],
            }
        ]

    def test_merges_non_standard_roles_with_error_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(role=MessageRole.AGENT, content="agent note"),
                ChatMessage(role=MessageRole.COMPACT, content="summary"),
            ],
        )
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            body = _ENGINE.build_body(request, _cfg())
        assert body["input"] == [
            {"role": "user", "content": "agent note"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "summary"}],
            },
        ]
        assert len(caplog.records) == 2

    def test_extra_body_merges_top_level_with_user_precedence(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            extra_body={"temperature": 0.0, "user_key": "req"},
        )
        body = _ENGINE.build_body(request, _cfg(extra_body={"cfg_key": 1, "user_key": "cfg"}))
        assert body["user_key"] == "req"
        assert body["cfg_key"] == 1
        assert body["temperature"] == 0.0


class TestToolMediaNativePlacement:
    """Tool-produced media: embed natively in the paired function_call_output —
    output becomes the array [input_text, input_image] keyed by call_id, with
    NO synthetic follow-up user input item."""

    _DATA_URL = "data:image/png;base64,aGVsbG8="

    def _tool_media_msg(self, call_id: str = "c1", name: str = "read") -> ChatMessage:
        return ChatMessage(
            role=MessageRole.TOOL,
            tool_call_id=call_id,
            name=name,
            content=[
                TextPart(text="[Image read: cat.png (image/png)]"),
                ImageUrlPart(image_url=ImageUrl(url=self._DATA_URL)),
            ],
        )

    def test_tool_media_embeds_natively_in_function_call_output(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        ToolCall(call_id="c1", tool_name="read", arguments={"path": "cat.png"})
                    ],
                ),
                self._tool_media_msg(),
                ChatMessage(role=MessageRole.USER, content="thanks"),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())

        assert body["input"] == [
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "read",
                "arguments": '{"path": "cat.png"}',
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [
                    {"type": "input_text", "text": "[Image read: cat.png (image/png)]"},
                    {"type": "input_image", "image_url": self._DATA_URL},
                ],
            },
            {"role": "user", "content": "thanks"},
        ]

    def test_tool_media_native_at_list_tail(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="read", arguments={})],
                ),
                self._tool_media_msg(),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())

        assert body["input"][-1] == {
            "type": "function_call_output",
            "call_id": "c1",
            "output": [
                {"type": "input_text", "text": "[Image read: cat.png (image/png)]"},
                {"type": "input_image", "image_url": self._DATA_URL},
            ],
        }

    def test_two_tool_runs_each_output_carries_own_media(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="read", arguments={})],
                ),
                self._tool_media_msg("c1"),
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c2", tool_name="read", arguments={})],
                ),
                self._tool_media_msg("c2"),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())

        outputs = [item for item in body["input"] if item.get("type") == "function_call_output"]
        assert len(outputs) == 2
        assert outputs[0]["call_id"] == "c1"
        assert outputs[0]["output"] == [
            {"type": "input_text", "text": "[Image read: cat.png (image/png)]"},
            {"type": "input_image", "image_url": self._DATA_URL},
        ]
        assert outputs[1]["call_id"] == "c2"
        assert outputs[1]["output"] == [
            {"type": "input_text", "text": "[Image read: cat.png (image/png)]"},
            {"type": "input_image", "image_url": self._DATA_URL},
        ]
        assert not any(item.get("role") == "user" for item in body["input"])
        item_kinds = [item.get("type") or "user" for item in body["input"]]
        assert item_kinds == [
            "function_call",
            "function_call_output",
            "function_call",
            "function_call_output",
        ]

    def test_interleaved_tool_batches_keep_media_with_own_call_id(self) -> None:
        # Given
        first_url = "data:image/png;base64,Zmlyc3Q="
        second_url = "data:image/png;base64,c2Vjb25k"
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="read_first", arguments={})],
                ),
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="c1",
                    name="read_first",
                    content=[
                        TextPart(text="first output"),
                        ImageUrlPart(image_url=ImageUrl(url=first_url)),
                    ],
                ),
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content="between batches",
                    tool_calls=[ToolCall(call_id="c2", tool_name="read_second", arguments={})],
                ),
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="c2",
                    name="read_second",
                    content=[
                        TextPart(text="second output"),
                        ImageUrlPart(image_url=ImageUrl(url=second_url)),
                    ],
                ),
            ],
        )

        # When
        body = _ENGINE.build_body(request, _cfg())

        # Then
        assert body["input"] == [
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "read_first",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [
                    {"type": "input_text", "text": "first output"},
                    {"type": "input_image", "image_url": first_url},
                ],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "between batches"}],
            },
            {
                "type": "function_call",
                "call_id": "c2",
                "name": "read_second",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "c2",
                "output": [
                    {"type": "input_text", "text": "second output"},
                    {"type": "input_image", "image_url": second_url},
                ],
            },
        ]

    def test_tail_tool_batch_output_carries_media_in_place(self) -> None:
        # Given
        first_url = "data:image/png;base64,Zmlyc3Q="
        second_url = "data:image/png;base64,c2Vjb25k"
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        ToolCall(call_id="c1", tool_name="read_first", arguments={}),
                        ToolCall(call_id="c2", tool_name="read_second", arguments={}),
                    ],
                ),
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="c1",
                    name="read_first",
                    content=[
                        TextPart(text="first output"),
                        ImageUrlPart(image_url=ImageUrl(url=first_url)),
                    ],
                ),
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="c2",
                    name="read_second",
                    content=[
                        TextPart(text="second output"),
                        ImageUrlPart(image_url=ImageUrl(url=second_url)),
                    ],
                ),
            ],
        )

        # When
        body = _ENGINE.build_body(request, _cfg())

        # Then
        assert body["input"][-2:] == [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [
                    {"type": "input_text", "text": "first output"},
                    {"type": "input_image", "image_url": first_url},
                ],
            },
            {
                "type": "function_call_output",
                "call_id": "c2",
                "output": [
                    {"type": "input_text", "text": "second output"},
                    {"type": "input_image", "image_url": second_url},
                ],
            },
        ]

    def test_tool_media_image_only_emits_image_array(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="read", arguments={})],
                ),
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="c1",
                    name="read",
                    content=[ImageUrlPart(image_url=ImageUrl(url=self._DATA_URL))],
                ),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())

        assert body["input"] == [
            {"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"},
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [{"type": "input_image", "image_url": self._DATA_URL}],
            },
        ]

    def test_text_only_tool_round_keeps_the_exact_body_shape(self) -> None:
        # Given
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(role=MessageRole.USER, content="start"),
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="lookup", arguments={})],
                ),
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="c1",
                    name="lookup",
                    content=[TextPart(text="plain"), TextPart(text=" text")],
                ),
                ChatMessage(role=MessageRole.ASSISTANT, content="done"),
            ],
        )

        # When
        body = _ENGINE.build_body(request, _cfg())

        # Then
        assert body == {
            "model": "gpt-5",
            "stream": True,
            "input": [
                {"role": "user", "content": "start"},
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "lookup",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "c1", "output": "plain text"},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            ],
            "store": True,
        }

    def test_tool_run_without_media_leaves_input_unchanged(self) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="read", arguments={})],
                ),
                ChatMessage(role=MessageRole.TOOL, content="text output", tool_call_id="c1"),
                ChatMessage(role=MessageRole.USER, content="thanks"),
            ],
        )
        body = _ENGINE.build_body(request, _cfg())

        assert [item.get("type") or "user" for item in body["input"]] == [
            "function_call",
            "function_call_output",
            "user",
        ]
        assert isinstance(body["input"][1]["output"], str)

    def test_unresolved_media_ref_in_tool_part_skipped_with_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = LLMRequest(
            model="gpt-5",
            messages=[
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[ToolCall(call_id="c1", tool_name="read", arguments={})],
                ),
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id="c1",
                    content=[
                        TextPart(text="hint"),
                        ImageUrlPart(image_url=ImageUrl(url="media://stale-aid")),
                    ],
                ),
            ],
        )
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            body = _ENGINE.build_body(request, _cfg())

        assert "media://" not in json.dumps(body)
        assert body["input"] == [
            {"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "hint"},
        ]
        assert any("stale-aid" in record.getMessage() for record in caplog.records)


class TestTransportSurfaces:
    def test_url_joins_responses_path(self) -> None:
        assert _ENGINE.url("https://api.openai.com/v1") == "https://api.openai.com/v1/responses"
        assert (
            _ENGINE.url("https://gateway.example.com/v1/")
            == "https://gateway.example.com/v1/responses"
        )

    def test_auth_headers_bearer_and_env_fallback_name(self) -> None:
        assert _ENGINE.auth_headers("sk-secret") == {"Authorization": "Bearer sk-secret"}
        assert _ENGINE.auth_headers(None) == {}
        assert _ENGINE.api_key_env == "OPENAI_API_KEY"
