from __future__ import annotations

from datetime import datetime

from modex_agent.core.message import ImageUrl, ImageUrlPart, MessageRole, TextPart, build_media_ref
from modex_agent.memory.pruned.render import render_transcript
from modex_agent.memory.xml_truncate import truncate_xml_safe

_CREATED_AT = datetime(2026, 8, 26, 12, 0)


def _render_parts(parts: list[TextPart | ImageUrlPart]) -> str:
    message = {
        "role": MessageRole.USER,
        "content": [part.model_dump(mode="json") for part in parts],
        "created_at": _CREATED_AT.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return render_transcript(1, "media", [message], _CREATED_AT, _CREATED_AT)


def test_media_ref_renders_as_shared_image_reference_line() -> None:
    # Given
    media_ref = build_media_ref("asset-123")

    # When
    rendered = _render_parts([ImageUrlPart(image_url=ImageUrl(url=media_ref))])

    # Then
    assert rendered.endswith("[image: media://asset-123]\n\n---")


def test_parts_render_to_string_before_xml_truncation() -> None:
    # Given
    rendered = _render_parts(
        [
            TextPart(text="inspect this image"),
            ImageUrlPart(image_url=ImageUrl(url=build_media_ref("asset-456"))),
        ]
    )

    # When
    truncated = truncate_xml_safe(rendered, max_chars=len(rendered))

    # Then
    assert truncated == rendered


def test_data_url_render_contains_no_base64_payload() -> None:
    # Given
    payload = "cGl4ZWw="
    part = ImageUrlPart(image_url=ImageUrl(url=f"data:image/png;base64,{payload}"))

    # When
    rendered = _render_parts([part])

    # Then
    assert rendered.endswith("[image: data:image/png, 5 bytes]\n\n---")
    assert payload not in rendered
