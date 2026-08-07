"""Unit tests for ToolResult unified content model (Step 3d).

Covers the post-migration ``content: list[ContentPart]`` source of truth:
``from_text`` factory, mixed TextPart/ImageUrlPart content, ``message_content()``
rendering, ``image_blocks`` / ``content_blocks`` computed views, and back-compat
for empty / error-only results.
"""

from __future__ import annotations

from modex_agent.core.message import ContentFormat, ImageUrl, ImageUrlPart, TextPart
from modex_agent.core.tool_manager import ToolResult


class TestFromText:
    def test_from_text_produces_single_textpart(self):
        r = ToolResult.from_text("t", "hello")
        assert len(r.content) == 1
        assert isinstance(r.content[0], TextPart)
        assert r.content[0].text == "hello"

    def test_from_text_passes_kwargs(self):
        r = ToolResult.from_text("t", "hi", call_id="c1", execution_time=0.5)
        assert r.call_id == "c1"
        assert r.execution_time == 0.5


class TestMixedContent:
    def test_content_with_text_and_image(self):
        r = ToolResult(
            tool_name="read",
            content=[
                TextPart(text="[Image read: foo.png]"),
                ImageUrlPart(image_url=ImageUrl(url="data:image/png;base64,abc")),
            ],
        )
        assert len(r.content) == 2
        assert isinstance(r.content[0], TextPart)
        assert isinstance(r.content[1], ImageUrlPart)


class TestMessageContentRendering:
    def test_renders_only_textparts(self):
        r = ToolResult(
            tool_name="read",
            content=[
                TextPart(text="hello "),
                TextPart(text="world"),
                ImageUrlPart(image_url=ImageUrl(url="data:image/png;base64,abc")),
            ],
        )
        assert r.message_content() == "hello world"

    def test_empty_content_returns_empty(self):
        r = ToolResult(tool_name="t")
        assert r.message_content() == ""

    def test_error_only_returns_error_prefixed(self):
        r = ToolResult(tool_name="t", error="something failed")
        assert r.message_content() == "Error: something failed"

    def test_xml_content_returns_verbatim(self):
        xml = "<command_result><output>x</output></command_result>"
        r = ToolResult(
            tool_name="bash",
            content=[TextPart(text=xml)],
            content_format=ContentFormat.XML,
            truncatable_paths=["output"],
        )
        assert r.message_content() == xml


class TestImageBlocks:
    def test_returns_only_image_parts(self):
        r = ToolResult(
            tool_name="read",
            content=[
                TextPart(text="hint"),
                ImageUrlPart(image_url=ImageUrl(url="data:image/png;base64,abc")),
            ],
        )
        assert len(r.image_blocks) == 1
        assert r.image_blocks[0].image_url.url == "data:image/png;base64,abc"

    def test_empty_when_no_images(self):
        r = ToolResult.from_text("t", "text only")
        assert r.image_blocks == []


class TestContentBlocksComputed:
    def test_returns_wire_dicts_for_images(self):
        r = ToolResult(
            tool_name="read",
            content=[
                TextPart(text="hint"),
                ImageUrlPart(image_url=ImageUrl(url="data:image/png;base64,abc")),
            ],
        )
        blocks = r.content_blocks
        assert blocks is not None
        assert len(blocks) == 1
        assert blocks[0]["type"] == "image_url"
        assert blocks[0]["image_url"]["url"] == "data:image/png;base64,abc"

    def test_none_when_no_images(self):
        r = ToolResult.from_text("t", "text only")
        assert r.content_blocks is None

    def test_includes_detail_when_set(self):
        r = ToolResult(
            tool_name="read",
            content=[
                ImageUrlPart(image_url=ImageUrl(url="data:image/png;base64,abc", detail="high")),
            ],
        )
        blocks = r.content_blocks
        assert blocks is not None
        assert blocks[0]["image_url"]["detail"] == "high"


class TestRoundTrip:
    """Pydantic round-trip under ``extra="forbid"`` with computed fields.

    The ``_strip_computed_fields`` before-validator strips ``image_blocks`` /
    ``content_blocks`` (which ``@computed_field`` serializes into
    ``model_dump``) so ``extra="forbid"`` doesn't reject them on re-validation.
    """

    def test_tool_result_model_dump_round_trip(self):
        original = ToolResult(
            tool_name="read",
            content=[
                TextPart(text="[Image read: test.png]"),
                ImageUrlPart(image_url=ImageUrl(url="data:image/png;base64,abc")),
            ],
            call_id="call_123",
            execution_time=1.5,
        )
        dumped = original.model_dump()
        assert "image_blocks" in dumped
        assert "content_blocks" in dumped

        restored = ToolResult.model_validate(dumped)
        assert restored.tool_name == "read"
        assert restored.call_id == "call_123"
        assert len(restored.content) == 2
        assert isinstance(restored.content[0], TextPart)
        assert isinstance(restored.content[1], ImageUrlPart)
        assert restored.content[1].image_url.url == "data:image/png;base64,abc"
        assert restored.image_blocks == original.image_blocks

    def test_tool_result_json_round_trip(self):
        original = ToolResult.from_text("bash", "hello world", call_id="c1")
        json_str = original.model_dump_json()
        restored = ToolResult.model_validate_json(json_str)
        assert restored.tool_name == "bash"
        assert restored.message_content() == "hello world"
        assert restored.call_id == "c1"
