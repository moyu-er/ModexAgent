"""Unit tests for ``build_inline_image_block`` (ADR-0013 §4 / Unit 4).

Verifies the provider-side renderer produces the caption + image_url tail
pair, that the caption literal matches byte-for-byte, and that the base64
payload round-trips back to the original file bytes.
"""

from __future__ import annotations

import base64

from modex_agent.media.models import Attachment, AttachmentLocator, Kind
from modex_agent.utils.media_utils import build_inline_image_block


def _make_attachment(path: str, name: str, mime: str | None) -> Attachment:
    return Attachment(
        id="att-1",
        kind=Kind.IMAGE,
        name=name,
        mime=mime,
        size=0,
        path=path,
        locator=AttachmentLocator.WORKSPACE,
    )


class TestBuildInlineImageBlock:
    def test_caption_and_base64_roundtrip(self, tmp_path):
        # Minimal valid PNG: 1x1 transparent.
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        img_path = tmp_path / "cat.png"
        img_path.write_bytes(png_bytes)

        att = _make_attachment(str(img_path), "cat.png", "image/png")
        block = build_inline_image_block(att)

        assert isinstance(block, list)
        assert len(block) == 2

        # (a) caption text part with the exact literal.
        assert block[0] == {"type": "text", "text": "<image: cat.png>"}

        # (b) image_url block whose data URL decodes back to the file bytes.
        assert block[1]["type"] == "image_url"
        url = block[1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        b64_payload = url.split(",", 1)[1]
        assert base64.b64decode(b64_payload) == png_bytes

        # (c) the data-URL mime prefix matches the file's detected mime.
        assert url.startswith("data:image/png;base64,")

    def test_caption_uses_att_name_exactly(self, tmp_path):
        # JPEG magic bytes so detection gives image/jpeg even though name is unusual.
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 10
        img_path = tmp_path / "photo (1).jpeg"
        img_path.write_bytes(jpeg_bytes)

        att = _make_attachment(str(img_path), "photo (1).jpeg", "image/jpeg")
        block = build_inline_image_block(att)

        # Caption must carry the exact att.name, including spaces and parens.
        assert block[0]["text"] == "<image: photo (1).jpeg>"

    def test_missing_file_degrades_to_caption_with_missing_note(self, tmp_path):
        att = _make_attachment(str(tmp_path / "gone.png"), "gone.png", "image/png")
        block = build_inline_image_block(att)

        # No crash; caption preserved; second element is the <missing> note,
        # not an image_url block (matches ImageHandler's swallow-and-skip).
        assert len(block) == 2
        assert block[0] == {"type": "text", "text": "<image: gone.png>"}
        assert block[1]["type"] == "text"
        assert "missing" in block[1]["text"]
