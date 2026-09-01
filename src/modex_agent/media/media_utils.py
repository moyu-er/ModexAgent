"""Media utilities: image compression for model delivery (ADR-0013 media path).

The dormant ``MediaProcessor`` provider-side renderer family was removed
(2026-09): zero production callers across ADR-0013/0014 — v1 ships
mechanism B (path reference) only, and mechanism A, when activated, will
be built on the event-stream provider system (ADR-0046) rather than
revived from cold storage. The document-extraction optional dependencies
(pypdf/python-docx/openpyxl/python-pptx) went with it.

What remains is the live compression core: :func:`compress_image` and its
typed output :class:`CompressedImage`, consumed by the read tool (media
store READS subtree) and the LLM-boundary injection resolver.
"""

from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from PIL.Image import Resampling

try:
    from PIL import Image as _PILImage
except ImportError:
    _PILImage = None

_PILResampling: type[Resampling] | None
try:
    from PIL import Image as _PILImageForResampling

    _PILResampling = _PILImageForResampling.Resampling
except (ImportError, AttributeError):
    _PILResampling = None

_MAX_IMAGE_DIM = 2000
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_JPEG_QUALITIES = [85, 70, 55, 40]


class CompressedImage(BaseModel):
    """Model-ready image bytes — the compression core's typed output.

    ``data`` is the (possibly re-encoded) image payload and ``media_type``
    its authoritative MIME; consumed by the read tool (persisted into the
    media store READS subtree) and the injection resolver (data-URL build).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    data: bytes
    media_type: str


def compress_image(raw: bytes, source_mime: str) -> CompressedImage | None:
    """Compress raw image bytes for model delivery; ``None`` when undecodable.

    Semantics (idempotent pass-through):

    - Pillow unavailable → the original bytes pass through unchanged (best
      effort, matching the legacy data-URL degrade path).
    - Pillow decode failure (corrupt bytes) → ``None`` — the caller degrades
      to a text-only result; garbage never reaches the store or the wire.
    - Image already within the delivery budget (dimensions ≤
      ``_MAX_IMAGE_DIM`` AND decoded size ≤ ``_MAX_IMAGE_BYTES``) → returned
      unchanged. This is what makes the injection pass idempotent: READS
      snapshots are the output of this function, so re-running them through
      it is a no-op (no generation loss, no wasted re-encode).
    - Over budget → LANCZOS downscale to the dimension cap, then re-encode
      trying the original format first and JPEG at progressively lower
      qualities [85, 70, 55, 40] until the base64 payload fits
      ``_MAX_IMAGE_BYTES``; falls back to the smallest candidate produced.
    """
    if _PILImage is None or _PILResampling is None:
        return CompressedImage(data=raw, media_type=source_mime)

    try:
        img = _PILImage.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None

    if (
        img.width <= _MAX_IMAGE_DIM
        and img.height <= _MAX_IMAGE_DIM
        and len(raw) <= _MAX_IMAGE_BYTES
    ):
        return CompressedImage(data=raw, media_type=source_mime)

    if img.width > _MAX_IMAGE_DIM or img.height > _MAX_IMAGE_DIM:
        img.thumbnail((_MAX_IMAGE_DIM, _MAX_IMAGE_DIM), _PILResampling.LANCZOS)

    candidates: list[tuple[str, bytes]] = []
    buf = io.BytesIO()
    save_mime = (
        source_mime
        if source_mime in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp")
        else "image/png"
    )
    try:
        img.save(buf, format=save_mime.split("/")[1].upper())
        candidates.append((save_mime, buf.getvalue()))
    except Exception:
        pass
    for q in _JPEG_QUALITIES:
        buf = io.BytesIO()
        try:
            img.save(buf, format="JPEG", quality=q)
            candidates.append(("image/jpeg", buf.getvalue()))
        except Exception:
            continue

    if not candidates:
        return CompressedImage(data=raw, media_type=source_mime)

    for c_mime, encoded in candidates:
        if len(base64.b64encode(encoded)) <= _MAX_IMAGE_BYTES:
            return CompressedImage(data=encoded, media_type=c_mime)
    c_mime, encoded = candidates[-1]
    return CompressedImage(data=encoded, media_type=c_mime)


__all__ = ["CompressedImage", "compress_image"]
