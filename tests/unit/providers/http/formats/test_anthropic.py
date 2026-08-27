"""Tests for modex_agent.providers.http.formats.anthropic — Messages API engine.

Canned SSE frame sequences (mirroring real opencode recordings) are fed
straight into ``events``; ``build_body`` is asserted on its translated dict
output. Locks the anthropic-specific disciplines: every-turn thinking
replay (the opposite cadence from openai_compat), tool_result attachment
to the following user turn, the orphan-raises / dangling-drops pairing
boundary (ADR-0046), and replay delivery through ``Finish.replay`` only.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator

import pytest

from modex_agent.core.constants import FinishReason, ReasoningEffort
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.message import (
    ChatMessage,
    ImageUrl,
    ImageUrlPart,
    TextPart,
    build_media_ref,
)
from modex_agent.core.stream_events import (
    Finish,
    ReasoningDelta,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)
from modex_agent.core.types import MessageRole, TokenUsage, ToolCall
from modex_agent.providers.http.formats.anthropic import AnthropicProtocol, ProtocolStructureError
from modex_agent.providers.http.protocol import ProtocolConfig
from modex_agent.providers.http.sse import SseFrame

MODULE_LOGGER = "modex_agent.providers.http.formats.anthropic"


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _frame(event: str | None, payload: object) -> SseFrame:
    return SseFrame(event=event, data=payload if isinstance(payload, str) else json.dumps(payload))


def _frames(*frames: SseFrame) -> AsyncIterator[SseFrame]:
    """In-memory frame stream — zero network."""

    async def _gen() -> AsyncIterator[SseFrame]:
        for frame in frames:
            yield frame

    return _gen()


async def _run(*frames: SseFrame) -> list[object]:
    engine = AnthropicProtocol()
    return [event async for event in engine.events(_frames(*frames))]


def _cfg(**kwargs: object) -> ProtocolConfig:
    return ProtocolConfig(**kwargs)  # type: ignore[arg-type]


def _req(messages: list[ChatMessage], **kwargs: object) -> LLMRequest:
    kwargs.setdefault("model", "claude-sonnet-4-5")
    return LLMRequest(messages=messages, **kwargs)  # type: ignore[arg-type]


def _msg(role: MessageRole, content: object = None, **kwargs: object) -> ChatMessage:
    return ChatMessage(role=role, content=content, **kwargs)  # type: ignore[arg-type]


def _message_start(usage: dict[str, int]) -> SseFrame:
    return _frame(
        "message_start", {"type": "message_start", "message": {"role": "assistant", "usage": usage}}
    )


def _block_start(index: int, block: dict[str, object]) -> SseFrame:
    return _frame(
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": block},
    )


def _delta(index: int, delta: dict[str, object]) -> SseFrame:
    return _frame(
        "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}
    )


def _block_stop(index: int) -> SseFrame:
    return _frame("content_block_stop", {"type": "content_block_stop", "index": index})


def _message_delta(stop_reason: str, usage: dict[str, int]) -> SseFrame:
    return _frame(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": usage,
        },
    )


_MESSAGE_STOP = _frame("message_stop", {"type": "message_stop"})


def _finish_of(events: list[object]) -> Finish:
    assert isinstance(events[-1], Finish)
    return events[-1]


# ─── Scenario 1: plain text stream ───────────────────────────────────────────


class TestTextStream:
    async def test_text_stream_produces_deltas_usage_and_finish(self) -> None:
        events = await _run(
            _message_start({"input_tokens": 18, "output_tokens": 2}),
            _block_start(0, {"type": "text", "text": ""}),
            _delta(0, {"type": "text_delta", "text": "Hello"}),
            _delta(0, {"type": "text_delta", "text": "!"}),
            _block_stop(0),
            _message_delta("end_turn", {"input_tokens": 18, "output_tokens": 5}),
            _MESSAGE_STOP,
        )

        assert events[:2] == [TextDelta(text="Hello"), TextDelta(text="!")]
        snapshot = events[2]
        assert isinstance(snapshot, UsageSnapshot)
        assert snapshot.usage == TokenUsage(input_tokens=18, output_tokens=5)
        finish = _finish_of(events)
        assert finish.finish_reason is FinishReason.STOP
        assert finish.replay is None
        assert len(events) == 4


# ─── Scenario 2: thinking + signature stream (replay via Finish) ─────────────


class TestThinkingStream:
    async def test_thinking_and_signature_delivered_through_finish_replay(self) -> None:
        events = await _run(
            _message_start({"input_tokens": 10, "output_tokens": 1}),
            _block_start(0, {"type": "thinking", "thinking": ""}),
            _delta(0, {"type": "thinking_delta", "thinking": "Let me"}),
            _delta(0, {"type": "thinking_delta", "thinking": " think"}),
            _delta(0, {"type": "signature_delta", "signature": "sig-abc"}),
            _block_stop(0),
            _block_start(1, {"type": "text", "text": ""}),
            _delta(1, {"type": "text_delta", "text": "42"}),
            _block_stop(1),
            _message_delta("end_turn", {"output_tokens": 9}),
            _MESSAGE_STOP,
        )

        assert events[0] == ReasoningDelta(text="Let me")
        assert events[1] == ReasoningDelta(text=" think")
        assert events[2] == TextDelta(text="42")
        finish = _finish_of(events)
        assert finish.finish_reason is FinishReason.STOP
        assert finish.replay is not None
        assert finish.replay.reasoning_content == "Let me think"
        assert finish.replay.reasoning_signature == "sig-abc"
        assert finish.replay.reasoning_item_id is None
        assert finish.replay.reasoning_encrypted_content is None

    async def test_signature_without_thinking_text_still_replays_signature(self) -> None:
        events = await _run(
            _message_start({"input_tokens": 10}),
            _delta(0, {"type": "signature_delta", "signature": "sig-only"}),
            _message_delta("end_turn", {"output_tokens": 3}),
            _MESSAGE_STOP,
        )

        finish = _finish_of(events)
        assert finish.replay is not None
        assert finish.replay.reasoning_content is None
        assert finish.replay.reasoning_signature == "sig-only"


# ─── Scenario 3: tool_use input_json sharded stream ──────────────────────────


class TestToolUseStream:
    async def test_sharded_input_json_accumulates_into_tool_call_complete(self) -> None:
        events = await _run(
            _message_start({"input_tokens": 677, "output_tokens": 16}),
            _block_start(
                0, {"type": "tool_use", "id": "toolu_A", "name": "get_weather", "input": {}}
            ),
            _delta(0, {"type": "input_json_delta", "partial_json": ""}),
            _delta(0, {"type": "input_json_delta", "partial_json": '{"city":'}),
            _delta(0, {"type": "input_json_delta", "partial_json": ' "Paris"}'}),
            _block_stop(0),
            _message_delta("tool_use", {"output_tokens": 33}),
            _MESSAGE_STOP,
        )

        assert events[0] == ToolCallComplete(
            call_id="toolu_A", tool_name="get_weather", arguments={"city": "Paris"}
        )
        finish = _finish_of(events)
        assert finish.finish_reason is FinishReason.TOOL_CALLS

    async def test_tool_use_missing_block_stop_finishes_at_message_stop(self) -> None:
        """A tool_use block whose content_block_stop never arrived still completes."""
        events = await _run(
            _message_start({"input_tokens": 100}),
            _block_start(
                0, {"type": "tool_use", "id": "toolu_A", "name": "get_weather", "input": {}}
            ),
            _delta(0, {"type": "input_json_delta", "partial_json": '{"city": "Paris"}'}),
            _block_start(1, {"type": "tool_use", "id": "toolu_B", "name": "now", "input": {}}),
            _message_delta("tool_use", {"output_tokens": 50}),
            _MESSAGE_STOP,
        )

        assert events[0] == ToolCallComplete(
            call_id="toolu_A", tool_name="get_weather", arguments={"city": "Paris"}
        )
        # Zero-argument call (no delta ever arrived) finishes with arguments={}.
        assert events[1] == ToolCallComplete(call_id="toolu_B", tool_name="now", arguments={})
        assert isinstance(events[2], UsageSnapshot)
        assert _finish_of(events).finish_reason is FinishReason.TOOL_CALLS


# ─── Scenario 4: usage stitching (message_start input + message_delta output) ─


class TestUsageStitching:
    async def test_input_from_start_and_output_from_delta(self) -> None:
        events = await _run(
            _message_start(
                {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 2,
                }
            ),
            _message_delta("end_turn", {"input_tokens": 100, "output_tokens": 42}),
            _MESSAGE_STOP,
        )

        snapshot = events[0]
        assert isinstance(snapshot, UsageSnapshot)
        assert snapshot.usage == TokenUsage(
            input_tokens=100,
            cache_read_input_tokens=20,
            cache_creation_input_tokens=5,
            output_tokens=42,
        )
        assert snapshot.usage.total_tokens == 167

    async def test_usage_without_message_delta_keeps_start_values(self) -> None:
        events = await _run(
            _message_start({"input_tokens": 7, "output_tokens": 3}),
            _MESSAGE_STOP,
        )

        snapshot = events[0]
        assert isinstance(snapshot, UsageSnapshot)
        assert snapshot.usage == TokenUsage(input_tokens=7, output_tokens=3)


# ─── Scenario 5: error frames ─────────────────────────────────────────────────


class TestErrorFrames:
    @pytest.mark.parametrize(
        ("error_type", "expected_kind", "should_retry"),
        [
            ("overloaded_error", "server", True),
            ("invalid_request_error", "invalid_request", False),
            ("api_error", "unknown", False),
        ],
    )
    async def test_error_frame_maps_to_stream_failure(
        self, error_type: str, expected_kind: str, should_retry: bool
    ) -> None:
        events = await _run(
            _frame(
                "error",
                {"type": "error", "error": {"type": error_type, "message": "boom"}},
            )
        )

        assert len(events) == 1
        failure = events[0]
        assert isinstance(failure, StreamFailure)
        assert failure.error_info.kind.value == expected_kind
        assert failure.error_info.message == "boom"
        assert failure.error_info.should_retry is should_retry

    async def test_error_payload_without_event_line_also_fails(self) -> None:
        """A 200-with-JSON-body arrives as one event-less frame; dispatch on data.type."""
        events = await _run(
            SseFrame(
                data=json.dumps(
                    {"type": "error", "error": {"type": "overloaded_error", "message": "o"}}
                )
            )
        )

        failure = events[0]
        assert isinstance(failure, StreamFailure)
        assert failure.error_info.kind.value == "server"


# ─── Scenario 6: ping and unknown frames are skipped ─────────────────────────


class TestFrameTolerance:
    async def test_ping_and_unknown_data_types_are_skipped(self) -> None:
        events = await _run(
            _frame("ping", {"type": "ping"}),
            _frame(None, {"type": "ping"}),  # no event line, data.type dispatch
            _frame("mystery_event", {"type": "mystery"}),
            _frame(None, {"no_type_key": 1}),
        )

        assert events == []

    async def test_malformed_json_frame_yields_stream_failure(self) -> None:
        events = await _run(_frame("content_block_delta", "{not json"))

        assert len(events) == 1
        failure = events[0]
        assert isinstance(failure, StreamFailure)
        assert failure.error_info.kind.value == "invalid_request"

    async def test_tool_stream_grammar_violation_yields_stream_failure(self) -> None:
        """input_json_delta with no preceding content_block_start — fail loud."""
        events = await _run(
            _message_start({"input_tokens": 5}),
            _delta(0, {"type": "input_json_delta", "partial_json": '{"x": 1}'}),
        )

        assert len(events) == 1
        failure = events[0]
        assert isinstance(failure, StreamFailure)
        assert failure.error_info.kind.value == "invalid_request"


# ─── Scenario 7: build_body message translation ───────────────────────────────


class TestBuildBodyMessages:
    def test_system_messages_extracted_to_top_level_joined(self) -> None:
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(MessageRole.SYSTEM, "You are helpful."),
                    _msg(MessageRole.SYSTEM, "Be terse."),
                    _msg(MessageRole.USER, "hi"),
                ]
            ),
            _cfg(),
        )

        assert body["system"] == "You are helpful.\n\nBe terse."
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]

    def test_tool_result_attaches_to_following_user_turn_blocks_first(self) -> None:
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(MessageRole.USER, "hi"),
                    _msg(
                        MessageRole.ASSISTANT,
                        tool_calls=[
                            ToolCall(
                                call_id="t1", tool_name="get_weather", arguments={"city": "Paris"}
                            )
                        ],
                    ),
                    _msg(MessageRole.TOOL, "20C", tool_call_id="t1"),
                    _msg(MessageRole.USER, "thanks"),
                    _msg(MessageRole.USER, "more"),  # consecutive user merge
                ]
            ),
            _cfg(),
        )

        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "20C"},
                    {"type": "text", "text": "thanks"},
                    {"type": "text", "text": "more"},
                ],
            },
        ]

    def test_trailing_tool_results_form_their_own_user_turn(self) -> None:
        """The steady ReAct state: the request ends with TOOL messages."""
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(MessageRole.USER, "weather?"),
                    _msg(
                        MessageRole.ASSISTANT,
                        tool_calls=[
                            ToolCall(
                                call_id="t1", tool_name="get_weather", arguments={"city": "Paris"}
                            )
                        ],
                    ),
                    _msg(MessageRole.TOOL, "20C", tool_call_id="t1"),
                ]
            ),
            _cfg(),
        )

        assert body["messages"][-1] == {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "20C"}],
        }

    def test_tool_result_carries_native_image(self) -> None:
        """TOOL parts lower to native image blocks inside the tool_result block."""
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(
                        MessageRole.ASSISTANT,
                        tool_calls=[
                            ToolCall(call_id="t1", tool_name="read", arguments={"path": "cat.png"})
                        ],
                    ),
                    _msg(
                        MessageRole.TOOL,
                        [
                            TextPart(text="[Image read: cat.png (image/png)]"),
                            ImageUrlPart(image_url=ImageUrl(url="data:image/png;base64,aGVsbG8=")),
                        ],
                        tool_call_id="t1",
                    ),
                ]
            ),
            _cfg(),
        )

        assert body["messages"][-1] == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [
                        {"type": "text", "text": "[Image read: cat.png (image/png)]"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "aGVsbG8=",
                            },
                        },
                    ],
                }
            ],
        }

    def test_tool_result_data_url_preserves_exact_media_type_and_bytes(self) -> None:
        image_bytes = b"\xff\xd8\xff\xe0tool-result-image"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(
                        MessageRole.ASSISTANT,
                        tool_calls=[ToolCall(call_id="t1", tool_name="read", arguments={})],
                    ),
                    _msg(
                        MessageRole.TOOL,
                        [ImageUrlPart(image_url=ImageUrl(url=f"data:image/jpeg;base64,{encoded}"))],
                        tool_call_id="t1",
                    ),
                ]
            ),
            _cfg(),
        )

        tool_result = body["messages"][-1]["content"][0]
        assert tool_result["type"] == "tool_result"
        image_block = tool_result["content"][0]
        assert image_block["type"] == "image"
        source = image_block["source"]
        assert source["type"] == "base64"
        assert source["media_type"] == "image/jpeg"
        assert base64.b64decode(source["data"], validate=True) == image_bytes

    def test_tool_result_unresolved_media_ref_is_skipped_with_error_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            body = AnthropicProtocol().build_body(
                _req(
                    [
                        _msg(
                            MessageRole.ASSISTANT,
                            tool_calls=[ToolCall(call_id="t1", tool_name="read", arguments={})],
                        ),
                        _msg(
                            MessageRole.TOOL,
                            [
                                TextPart(text="image unavailable"),
                                ImageUrlPart(image_url=ImageUrl(url=build_media_ref("tool-image"))),
                            ],
                            tool_call_id="t1",
                        ),
                    ]
                ),
                _cfg(),
            )

        assert body["messages"][-1]["content"] == [
            {
                "type": "tool_result",
                "tool_use_id": "t1",
                "content": [{"type": "text", "text": "image unavailable"}],
            }
        ]
        assert [record.getMessage() for record in caplog.records] == [
            "anthropic engine: unresolved media:// reference reached the wire layer, "
            "part skipped: tool-image"
        ]

    def test_tool_result_before_assistant_turn_flushes_own_user_turn(self) -> None:
        """TOOL followed by assistant: the tool_result user turn stays adjacent to its tool_use."""
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(MessageRole.USER, "weather?"),
                    _msg(
                        MessageRole.ASSISTANT,
                        tool_calls=[ToolCall(call_id="t1", tool_name="get_weather", arguments={})],
                    ),
                    _msg(MessageRole.TOOL, "20C", tool_call_id="t1"),
                    _msg(MessageRole.ASSISTANT, "all done"),
                ]
            ),
            _cfg(),
        )

        assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user", "assistant"]
        assert body["messages"][2]["content"] == [
            {"type": "tool_result", "tool_use_id": "t1", "content": "20C"}
        ]

    def test_consecutive_assistant_messages_merge(self) -> None:
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(MessageRole.USER, "hi"),
                    _msg(MessageRole.ASSISTANT, "part one"),
                    _msg(MessageRole.ASSISTANT, "part two"),
                    _msg(MessageRole.USER, "ok"),
                ]
            ),
            _cfg(),
        )

        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "part one"},
                    {"type": "text", "text": "part two"},
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "ok"}]},
        ]

    def test_thinking_replays_on_every_assistant_turn(self) -> None:
        """anthropic cadence — the opposite of compat's tool-call-turns-only rule.

        Both assistant turns carry content + signature; the FIRST one has no
        tool_calls, which is exactly where compat would NOT replay.
        """
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(MessageRole.USER, "q"),
                    _msg(
                        MessageRole.ASSISTANT,
                        "a1",
                        reasoning_content="thinking 1",
                        reasoning_signature="sig-1",
                    ),
                    _msg(MessageRole.USER, "q2"),
                    _msg(
                        MessageRole.ASSISTANT,
                        "a2",
                        reasoning_content="thinking 2",
                        reasoning_signature="sig-2",
                        tool_calls=[ToolCall(call_id="t9", tool_name="now", arguments={})],
                    ),
                ]
            ),
            _cfg(),
        )

        assistant_turns = [m for m in body["messages"] if m["role"] == "assistant"]
        assert assistant_turns[0]["content"][0] == {
            "type": "thinking",
            "thinking": "thinking 1",
            "signature": "sig-1",
        }
        assert assistant_turns[1]["content"][0] == {
            "type": "thinking",
            "thinking": "thinking 2",
            "signature": "sig-2",
        }

    def test_thinking_without_signature_not_replayed_and_logs_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            body = AnthropicProtocol().build_body(
                _req(
                    [
                        _msg(MessageRole.USER, "q"),
                        _msg(MessageRole.ASSISTANT, "a", reasoning_content="orphan thinking"),
                    ]
                ),
                _cfg(),
            )

        assert body["messages"][1]["content"] == [{"type": "text", "text": "a"}]
        assert "content+signature" in caplog.text

    def test_non_standard_role_merged_with_error_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            body = AnthropicProtocol().build_body(
                _req(
                    [
                        _msg(MessageRole.USER, "hi"),
                        _msg(MessageRole.AGENT, "agent note"),
                    ]
                ),
                _cfg(),
            )

        assert body["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "text", "text": "agent note"},
                ],
            }
        ]
        assert "merged" in caplog.text

    def test_governance_fields_never_reach_the_wire(self) -> None:
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(
                        MessageRole.USER,
                        "hi",
                        token_count=42,
                        truncatable_paths=["/a"],
                    )
                ]
            ),
            _cfg(),
        )

        assert set(body) <= {
            "model",
            "system",
            "messages",
            "max_tokens",
            "tools",
            "temperature",
            "top_p",
            "stop_sequences",
            "thinking",
            "stream",
            "tool_choice",
        }
        assert "token_count" not in json.dumps(body)


# ─── Scenario 8: build_body request parameters ────────────────────────────────


class TestBuildBodyParameters:
    def test_max_tokens_fallback_chain(self) -> None:
        engine = AnthropicProtocol()
        messages = [_msg(MessageRole.USER, "hi")]

        assert engine.build_body(_req(messages, max_output_tokens=100), _cfg())["max_tokens"] == 100
        assert engine.build_body(_req(messages), _cfg(max_output_tokens=2048))["max_tokens"] == 2048
        assert engine.build_body(_req(messages), _cfg())["max_tokens"] == 8192

    @pytest.mark.parametrize(
        ("effort", "expected_budget"),
        [
            (ReasoningEffort.NONE, None),
            (ReasoningEffort.MINIMAL, 1024),
            (ReasoningEffort.LOW, 1024),
            (ReasoningEffort.MEDIUM, 4096),
            (ReasoningEffort.HIGH, 16384),
            (ReasoningEffort.XHIGH, 16384),
            (ReasoningEffort.MAX, 16384),
        ],
    )
    def test_thinking_budget_mapping(
        self, effort: ReasoningEffort, expected_budget: int | None
    ) -> None:
        body = AnthropicProtocol().build_body(
            _req([_msg(MessageRole.USER, "hi")], reasoning_effort=effort),
            _cfg(),
        )

        if expected_budget is None:
            assert "thinking" not in body
        else:
            assert body["thinking"] == {"type": "enabled", "budget_tokens": expected_budget}

    def test_config_reasoning_effort_used_when_request_is_none(self) -> None:
        body = AnthropicProtocol().build_body(
            _req([_msg(MessageRole.USER, "hi")]),
            _cfg(reasoning_effort=ReasoningEffort.MEDIUM),
        )

        assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}

    def test_extra_body_thinking_overrides_precisely(self) -> None:
        override = {"type": "enabled", "budget_tokens": 555}
        body = AnthropicProtocol().build_body(
            _req(
                [_msg(MessageRole.USER, "hi")],
                reasoning_effort=ReasoningEffort.HIGH,
                extra_body={"thinking": override},
            ),
            _cfg(),
        )

        assert body["thinking"] == override

    def test_extra_body_thinking_enables_when_effort_none(self) -> None:
        override = {"type": "enabled", "budget_tokens": 777}
        body = AnthropicProtocol().build_body(
            _req([_msg(MessageRole.USER, "hi")], extra_body={"thinking": override}),
            _cfg(),
        )

        assert body["thinking"] == override

    def test_temperature_above_one_clamped_with_error_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            body = AnthropicProtocol().build_body(
                _req([_msg(MessageRole.USER, "hi")], temperature=1.5),
                _cfg(),
            )

        assert body["temperature"] == 1.0
        assert "clamped" in caplog.text

    def test_sampling_parameters_passthrough(self) -> None:
        body = AnthropicProtocol().build_body(
            _req(
                [_msg(MessageRole.USER, "hi")],
                temperature=0.5,
                top_p=0.9,
                stop=("STOP", "END"),
            ),
            _cfg(),
        )

        assert body["temperature"] == 0.5
        assert body["top_p"] == 0.9
        assert body["stop_sequences"] == ["STOP", "END"]

    def test_optional_parameters_omitted_when_unset(self) -> None:
        body = AnthropicProtocol().build_body(
            _req([_msg(MessageRole.USER, "hi")]),
            _cfg(),
        )

        for key in ("temperature", "top_p", "stop_sequences", "thinking", "tools", "system"):
            assert key not in body
        assert body["stream"] is True
        assert body["model"] == "claude-sonnet-4-5"

    def test_tools_lowered_to_flat_schema_and_tool_choice_auto(self) -> None:
        body = AnthropicProtocol().build_body(
            _req(
                [_msg(MessageRole.USER, "hi")],
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
            ),
            _cfg(),
        )

        assert body["tools"] == [
            {
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ]
        assert body["tool_choice"] == {"type": "auto"}


# ─── Scenario 9: structural pairing failures ──────────────────────────────────


class TestStructuralPairing:
    def test_orphan_tool_result_with_parts_raises_protocol_structure_error(self) -> None:
        with pytest.raises(ProtocolStructureError, match="t_missing"):
            AnthropicProtocol().build_body(
                _req(
                    [
                        _msg(
                            MessageRole.TOOL,
                            [
                                TextPart(text="image result"),
                                ImageUrlPart(
                                    image_url=ImageUrl(url="data:image/png;base64,aW1hZ2UtcmVzdWx0")
                                ),
                            ],
                            tool_call_id="t_missing",
                        )
                    ]
                ),
                _cfg(),
            )

    def test_orphan_tool_result_raises_protocol_structure_error(self) -> None:
        with pytest.raises(ProtocolStructureError, match="t_missing"):
            AnthropicProtocol().build_body(
                _req(
                    [
                        _msg(MessageRole.USER, "hi"),
                        _msg(MessageRole.TOOL, "20C", tool_call_id="t_missing"),
                    ]
                ),
                _cfg(),
            )

    def test_orphan_tool_result_without_any_tool_use_raises(self) -> None:
        with pytest.raises(ProtocolStructureError, match="tool_call_id None"):
            AnthropicProtocol().build_body(
                _req([_msg(MessageRole.TOOL, "20C")]),
                _cfg(),
            )

    def test_tool_result_before_its_tool_use_is_an_orphan(self) -> None:
        """Pairing requires the tool_use to come first."""
        with pytest.raises(ProtocolStructureError):
            AnthropicProtocol().build_body(
                _req(
                    [
                        _msg(MessageRole.TOOL, "20C", tool_call_id="t1"),
                        _msg(
                            MessageRole.ASSISTANT,
                            tool_calls=[
                                ToolCall(call_id="t1", tool_name="get_weather", arguments={})
                            ],
                        ),
                    ]
                ),
                _cfg(),
            )

    def test_dangling_tail_tool_use_dropped_with_error_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            body = AnthropicProtocol().build_body(
                _req(
                    [
                        _msg(MessageRole.USER, "hi"),
                        _msg(
                            MessageRole.ASSISTANT,
                            "let me check",
                            tool_calls=[
                                ToolCall(
                                    call_id="t1",
                                    tool_name="get_weather",
                                    arguments={"city": "Paris"},
                                ),
                                ToolCall(call_id="t2", tool_name="now", arguments={}),
                            ],
                        ),
                        _msg(MessageRole.USER, "and then?"),  # no TOOL results follow
                    ]
                ),
                _cfg(),
            )

        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "let me check"}]},
            {"role": "user", "content": [{"type": "text", "text": "and then?"}]},
        ]
        assert "dangling" in caplog.text
        assert "t1" in caplog.text and "t2" in caplog.text

    def test_partially_answered_tool_calls_only_drop_the_dangling_one(self) -> None:
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(MessageRole.USER, "hi"),
                    _msg(
                        MessageRole.ASSISTANT,
                        tool_calls=[
                            ToolCall(call_id="t1", tool_name="get_weather", arguments={}),
                            ToolCall(call_id="t2", tool_name="now", arguments={}),
                        ],
                    ),
                    _msg(MessageRole.TOOL, "20C", tool_call_id="t1"),
                ]
            ),
            _cfg(),
        )

        assistant_content = body["messages"][1]["content"]
        assert assistant_content == [
            {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}}
        ]
        assert body["messages"][2]["content"] == [
            {"type": "tool_result", "tool_use_id": "t1", "content": "20C"}
        ]


# ─── Scenario 10: image lowering and URL join ─────────────────────────────────


class TestImagesAndUrl:
    def test_data_url_lowers_to_base64_source(self) -> None:
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(
                        MessageRole.USER,
                        [
                            {"type": "text", "text": "what is this?"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,aGVsbG8="},
                            },
                        ],
                    )
                ]
            ),
            _cfg(),
        )

        assert body["messages"][0]["content"] == [
            {"type": "text", "text": "what is this?"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
            },
        ]

    def test_unresolved_media_refs_are_skipped_with_error_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = _req(
            [
                _msg(
                    MessageRole.SYSTEM,
                    [
                        {"type": "text", "text": "system"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "media://system-image"},
                        },
                    ],
                ),
                _msg(
                    MessageRole.USER,
                    [
                        {"type": "text", "text": "user"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "media://user-image"},
                        },
                    ],
                ),
            ]
        )

        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            body = AnthropicProtocol().build_body(request, _cfg())

        assert "media://" not in json.dumps(body)
        assert body["system"] == "system"
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "user"}]}]
        assert [record.getMessage() for record in caplog.records] == [
            "anthropic engine: unresolved media:// reference reached the wire layer, "
            "part skipped: system-image",
            "anthropic engine: unresolved media:// reference reached the wire layer, "
            "part skipped: user-image",
        ]

    def test_http_url_lowers_to_url_source(self) -> None:
        body = AnthropicProtocol().build_body(
            _req(
                [
                    _msg(
                        MessageRole.USER,
                        [
                            ImageUrlPart(image_url=ImageUrl(url="https://cdn.example.com/cat.png")),
                        ],
                    )
                ]
            ),
            _cfg(),
        )

        assert body["messages"][0]["content"] == [
            {
                "type": "image",
                "source": {"type": "url", "url": "https://cdn.example.com/cat.png"},
            }
        ]

    def test_unsupported_image_scheme_skipped_with_error_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            body = AnthropicProtocol().build_body(
                _req(
                    [
                        _msg(
                            MessageRole.USER,
                            [
                                {"type": "text", "text": "look"},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "ftp://example.com/cat.png"},
                                },
                            ],
                        )
                    ]
                ),
                _cfg(),
            )

        assert body["messages"][0]["content"] == [{"type": "text", "text": "look"}]
        assert "unsupported scheme" in caplog.text

    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
            ("https://api.anthropic.com/v1/", "https://api.anthropic.com/v1/messages"),
            ("https://gateway.example.com", "https://gateway.example.com/v1/messages"),
            ("https://gateway.example.com/", "https://gateway.example.com/v1/messages"),
        ],
    )
    def test_url_join(self, base_url: str, expected: str) -> None:
        assert AnthropicProtocol().url(base_url) == expected


# ─── Scenario 11: auth headers and stop-reason mapping ────────────────────────


class TestAuthAndStopReasons:
    def test_auth_headers_carry_version_unconditionally(self) -> None:
        engine = AnthropicProtocol()
        assert engine.auth_headers("sk-ant-x") == {
            "x-api-key": "sk-ant-x",
            "anthropic-version": "2023-06-01",
        }
        assert engine.auth_headers(None) == {"anthropic-version": "2023-06-01"}
        assert engine.auth_headers("") == {"anthropic-version": "2023-06-01"}

    def test_api_key_env(self) -> None:
        assert AnthropicProtocol().api_key_env == "ANTHROPIC_API_KEY"

    @pytest.mark.parametrize(
        ("stop_reason", "expected"),
        [
            ("end_turn", FinishReason.STOP),
            ("stop_sequence", FinishReason.STOP),
            ("tool_use", FinishReason.TOOL_CALLS),
            ("max_tokens", FinishReason.LENGTH),
            ("refusal", FinishReason.CONTENT_FILTER),
            ("pause_turn", FinishReason.STOP),
        ],
    )
    async def test_stop_reason_mapping(self, stop_reason: str, expected: FinishReason) -> None:
        events = await _run(
            _message_start({"input_tokens": 1}),
            _message_delta(stop_reason, {"output_tokens": 2}),
            _MESSAGE_STOP,
        )

        assert _finish_of(events).finish_reason is expected

    async def test_pause_turn_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            await _run(
                _message_start({"input_tokens": 1}),
                _message_delta("pause_turn", {"output_tokens": 2}),
                _MESSAGE_STOP,
            )

        assert "pause_turn" in caplog.text
