"""Unit tests for ChatMessage to_dict/from_dict tool_calls serialization and
ContentPart discriminated union validation.

Covers the OpenAI wire-format tool_calls round-trip and the multimodal
content part (TextPart / ImageUrlPart) discriminated union on ChatMessage.
"""
from __future__ import annotations

import base64
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from modex_agent.agents.react.message_builder import build_assistant_message
from modex_agent.core import message
from modex_agent.core.capabilities import Modality
from modex_agent.core.message import (
    ChatMessage,
    ContentPartType,
    ImageUrl,
    ImageUrlPart,
    TextPart,
)
from modex_agent.core.types import MessageRole, ToolCall

# ---------------------------------------------------------------------------
# #1 — ChatMessage.to_dict / from_dict tool_calls (OpenAI wire format)
# ---------------------------------------------------------------------------


def test_to_dict_tool_calls_openai_wire_format():
    """to_dict serializes tool_calls into OpenAI wire format
    (id / type / function{name, arguments-as-JSON-string})."""
    msg = ChatMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[ToolCall(tool_name="search", arguments={"q": "x"}, call_id="c1")],
    )
    result = msg.to_dict()
    assert result["tool_calls"] == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"q": "x"}'},
        }
    ]


def test_to_dict_tool_calls_empty_arguments():
    """Empty arguments dict serializes to the literal string "{}"."""
    msg = ChatMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[ToolCall(tool_name="t", arguments={})],
    )
    result = msg.to_dict()
    assert result["tool_calls"][0]["function"]["arguments"] == "{}"


def test_to_dict_tool_calls_none_call_id():
    """When call_id is None, to_dict auto-generates an id as ``call_{index}``."""
    msg = ChatMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[ToolCall(tool_name="t", arguments={}, call_id=None)],
    )
    result = msg.to_dict()
    assert result["tool_calls"][0]["id"] == "call_0"


def test_from_dict_openai_format_tool_calls():
    """from_dict parses OpenAI wire-format tool_calls back into ToolCall."""
    msg = ChatMessage.from_dict(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q": "x"}'},
                }
            ],
        }
    )
    assert msg.tool_calls is not None
    assert msg.tool_calls[0].tool_name == "search"
    assert msg.tool_calls[0].arguments == {"q": "x"}
    assert msg.tool_calls[0].call_id == "c1"


def test_from_dict_tool_calls_arguments_as_dict():
    """from_dict passes through arguments that are already a dict (no JSON parse)."""
    msg = ChatMessage.from_dict(
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": {"a": 1}}}
            ],
        }
    )
    assert msg.tool_calls is not None
    assert msg.tool_calls[0].arguments == {"a": 1}


def test_from_dict_tool_calls_missing_id():
    """from_dict sets call_id to None when the OpenAI id field is absent."""
    msg = ChatMessage.from_dict(
        {
            "role": "assistant",
            "tool_calls": [
                {"type": "function", "function": {"name": "t", "arguments": "{}"}}
            ],
        }
    )
    assert msg.tool_calls is not None
    assert msg.tool_calls[0].call_id is None


def test_from_dict_tool_calls_empty_string_arguments():
    """from_dict converts an empty-string arguments to an empty dict."""
    msg = ChatMessage.from_dict(
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": ""}}
            ],
        }
    )
    assert msg.tool_calls is not None
    assert msg.tool_calls[0].arguments == {}


def test_to_dict_persists_reasoning_content():
    """to_dict keeps reasoning_content so the thinking-mode passback survives
    persistence (compaction / process restarts).

    reasoning_content is now a declared field (ADR-0046); this injects it
    via ``__pydantic_extra__`` to prove the legacy extra-key storage form
    still dumps the same ``reasoning_content`` key.
    """
    msg = ChatMessage(role=MessageRole.ASSISTANT, content="hi")
    msg.__pydantic_extra__ = {"reasoning_content": "thinking"}
    d = msg.to_dict()
    assert d["reasoning_content"] == "thinking"
    assert d["content"] == "hi"


class TestReasoningContentRoundTrip:
    """to_dict/from_dicts must keep reasoning_content across the storage
    round-trip so the DeepSeek thinking-mode passback survives compaction
    and process restarts."""

    def test_round_trip_tool_call_turn(self):
        msg = build_assistant_message(
            None,
            [ToolCall(tool_name="search", arguments={"q": "x"}, call_id="c1")],
            reasoning_content="cot",
        )
        rehydrated = ChatMessage.from_dicts([msg.to_dict()])[0]
        assert rehydrated.reasoning_content == "cot"
        assert rehydrated.tool_calls is not None
        assert rehydrated.tool_calls[0].call_id == "c1"

    def test_round_trip_plain_assistant_turn(self):
        """Plain turn (no tool_calls) round-trips reasoning too — the
        provider layer decides whether to replay it on the wire."""
        msg = build_assistant_message("answer", [], reasoning_content="cot")
        rehydrated = ChatMessage.from_dicts([msg.to_dict()])[0]
        assert rehydrated.reasoning_content == "cot"


# ---------------------------------------------------------------------------
# #1b — reasoning declared fields (ADR-0046): storage zero-migration
# ---------------------------------------------------------------------------


class TestReasoningDeclaredFields:
    """ADR-0046: reasoning_content promoted from model_extra to a declared
    field with the same persisted key — old rows read back unchanged, new
    constructions serialize identically."""

    def test_declared_field_to_dict_matches_legacy_extra_form(self):
        """Core storage-zero-migration proof: the declared-field construction
        and the legacy extra-key injection produce identical to_dict output
        (same keys, same values) — persisted shape is byte-compatible."""
        declared = ChatMessage(role=MessageRole.ASSISTANT, content="x", reasoning_content="r")
        legacy = ChatMessage(role=MessageRole.ASSISTANT, content="x")
        legacy.__pydantic_extra__ = {"reasoning_content": "r"}
        assert declared.to_dict() == legacy.to_dict()

    def test_legacy_extra_dict_lands_in_declared_field(self):
        """A dict carrying reasoning_content as an (old-style) extra key
        validates into the declared field — same-name declared fields win
        over model_extra under extra='allow'."""
        msg = ChatMessage.from_dict(
            {"role": "assistant", "content": "x", "reasoning_content": "r"}
        )
        assert msg.reasoning_content == "r"
        direct = ChatMessage.model_validate(
            {"role": "assistant", "content": "x", "reasoning_content": "r"}
        )
        assert direct.reasoning_content == "r"

    def test_user_message_field_is_none_and_key_absent(self):
        msg = ChatMessage(role=MessageRole.USER, content="x")
        assert msg.reasoning_content is None
        assert "reasoning_content" not in msg.to_dict()

    def test_legacy_row_round_trips_all_four_fields(self):
        """A persisted row carrying all four reasoning keys survives the
        from_dict round-trip with every field attribute-accessible."""
        row = {
            "role": "assistant",
            "content": "x",
            "reasoning_content": "r",
            "reasoning_signature": "sig",
            "reasoning_item_id": "item_1",
            "reasoning_encrypted_content": "enc",
        }
        rehydrated = ChatMessage.from_dict(dict(row))
        assert rehydrated.reasoning_content == "r"
        assert rehydrated.reasoning_signature == "sig"
        assert rehydrated.reasoning_item_id == "item_1"
        assert rehydrated.reasoning_encrypted_content == "enc"
        dumped = rehydrated.to_dict()
        assert dumped["reasoning_content"] == "r"
        assert dumped["reasoning_signature"] == "sig"
        assert dumped["reasoning_item_id"] == "item_1"
        assert dumped["reasoning_encrypted_content"] == "enc"


# ---------------------------------------------------------------------------
# #2 — ContentPart discriminated union (TextPart / ImageUrlPart)
# ---------------------------------------------------------------------------


def test_text_part_construction():
    part = TextPart(text="hello")
    assert part.type == ContentPartType.TEXT
    assert part.text == "hello"


def test_image_url_part_construction():
    part = ImageUrlPart(image_url=ImageUrl(url="https://x", detail="high"))
    assert part.type == ContentPartType.IMAGE_URL
    assert part.image_url.url == "https://x"
    assert part.image_url.detail == "high"


def test_chatmessage_content_list_contentpart():
    msg = ChatMessage(role=MessageRole.USER, content=[TextPart(text="a"), TextPart(text="b")])
    assert isinstance(msg.content, list)
    assert len(msg.content) == 2
    assert all(isinstance(p, TextPart) for p in msg.content)


def test_chatmessage_content_dict_validates_to_contentpart():
    msg = ChatMessage(role=MessageRole.USER, content=[{"type": "text", "text": "hi"}])  # type: ignore[arg-type]
    assert isinstance(msg.content, list)
    assert isinstance(msg.content[0], TextPart)
    assert msg.content[0].text == "hi"


def test_chatmessage_invalid_contentpart_type_rejected():
    with pytest.raises(ValidationError):
        ChatMessage(role=MessageRole.USER, content=[{"type": "unknown"}])  # type: ignore[arg-type]


def test_to_dict_content_list_contentpart():
    msg = ChatMessage(role=MessageRole.USER, content=[TextPart(text="hi")])
    result = msg.to_dict()
    assert result["content"] == [{"type": "text", "text": "hi"}]


@pytest.mark.parametrize(
    ("part", "expected"),
    [
        (TextPart(text="hello"), Modality.TEXT),
        (ImageUrlPart(image_url=ImageUrl(url="https://x")), Modality.IMAGE),
    ],
)
def test_content_part_modality_maps_current_variants(part, expected):
    assert message.content_part_modality(part) == expected


def test_content_part_modality_rejects_future_variant():
    with pytest.raises(TypeError):
        message.content_part_modality(Mock(type="file"))


def test_media_ref_build_parse_round_trip():
    assert message.parse_media_ref(message.build_media_ref("attachment-1")) == "attachment-1"


@pytest.mark.parametrize("url", ["https://x", "media://"])
def test_parse_media_ref_rejects_non_reference(url):
    assert message.parse_media_ref(url) is None


def test_render_content_part_ref_text_and_image_urls():
    assert message.render_content_part_ref(TextPart(text="hello")) == "hello"
    media = ImageUrlPart(image_url=ImageUrl(url="media://attachment-1"))
    remote = ImageUrlPart(image_url=ImageUrl(url="https://x/image.png"))
    assert message.render_content_part_ref(media) == "[image: media://attachment-1]"
    assert message.render_content_part_ref(remote) == "[image: https://x/image.png]"


def test_render_content_part_ref_counts_decoded_data_bytes_exactly():
    known_bytes = b"known bytes"
    payload = base64.b64encode(known_bytes).decode("ascii")
    part = ImageUrlPart(image_url=ImageUrl(url=f"data:image/png;base64,{payload}"))
    assert message.render_content_part_ref(part) == f"[image: data:image/png, {len(known_bytes)} bytes]"


def test_render_content_part_ref_degrades_for_malformed_base64():
    part = ImageUrlPart(image_url=ImageUrl(url="data:image/png;base64,%%%"))
    assert message.render_content_part_ref(part) == "[image: data:image/png, ? bytes]"
