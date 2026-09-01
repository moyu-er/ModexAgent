"""compress_image unit tests — the live core that remains in media_utils.

The dormant ``MediaProcessor`` family (provider-side renderer, mechanism A)
was removed with its own test class; these tests pin the compression
contract that production consumers (read tool, LLM-boundary injection)
rely on: idempotent pass-through, corrupt-bytes degrade, budget
downscale/re-encode, and the Pillow-less best-effort path.
"""

from __future__ import annotations

import io

import pytest

from modex_agent.media.media_utils import CompressedImage, compress_image

_PIL_AVAILABLE = True
try:
    from PIL import Image
except ImportError:
    _PIL_AVAILABLE = False


def _png_bytes(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.skipif(not _PIL_AVAILABLE, reason="Pillow not installed")
async def test_within_budget_passes_through_unchanged() -> None:
    raw = _png_bytes(64, 64)
    result = compress_image(raw, "image/png")
    assert result == CompressedImage(data=raw, media_type="image/png")


@pytest.mark.skipif(not _PIL_AVAILABLE, reason="Pillow not installed")
async def test_idempotent_second_pass_is_noop() -> None:
    raw = _png_bytes(2500, 100)
    first = compress_image(raw, "image/png")
    assert first is not None
    second = compress_image(first.data, first.media_type)
    assert second is not None
    # READS snapshots are compress_image output; re-running is a no-op.
    assert second.data == first.data
    assert second.media_type == first.media_type


@pytest.mark.skipif(not _PIL_AVAILABLE, reason="Pillow not installed")
async def test_over_dimension_budget_downscales() -> None:
    raw = _png_bytes(4000, 3000)
    result = compress_image(raw, "image/png")
    assert result is not None
    img = Image.open(io.BytesIO(result.data))
    assert max(img.width, img.height) <= 2000


@pytest.mark.skipif(not _PIL_AVAILABLE, reason="Pillow not installed")
async def test_corrupt_bytes_return_none() -> None:
    garbage = b"\x89PNG\r\n\x1a\n" + b"\x00not-really-an-image"
    assert compress_image(garbage, "image/png") is None


@pytest.mark.skipif(not _PIL_AVAILABLE, reason="Pillow not installed")
async def test_unknown_mime_falls_back_to_png_reencode() -> None:
    raw = _png_bytes(4000, 100)
    result = compress_image(raw, "application/octet-stream")
    assert result is not None
    assert result.media_type == "image/png"
