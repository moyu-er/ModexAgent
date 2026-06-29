"""Magic-byte MIME sniffing + three-way kind classification (ADR-0013 §8).

Magic bytes are authoritative — extensions are a fallback only. ``Kind`` is the
shared substrate for both the v1 path-reference layer and the deferred
multimodal renderer, computed once at ingest.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Sequence

from modex_agent.media.models import Kind

# ---------------------------------------------------------------------------
# Magic-byte signatures. Ordered; first match wins. Each entry is a
# (offset, signature, mime) tuple. Only fixed-offset signatures live here —
# WEBP's tag is at offset 8 inside a RIFF container and is checked separately.
# ---------------------------------------------------------------------------
_WEBP_TAG_OFFSET: int = 8
_WEBP_TAG: bytes = b"WEBP"
_RIFF_TAG: bytes = b"RIFF"

_MAGIC_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"%PDF-", "application/pdf"),
)


def _sniff_magic(head: bytes) -> str | None:
    """Return the MIME implied by ``head``'s magic bytes, or ``None``."""
    for offset, signature, mime in _MAGIC_SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            return mime
    # WEBP: a RIFF container whose format tag is WEBP at offset 8.
    if (
        head[:4] == _RIFF_TAG
        and head[_WEBP_TAG_OFFSET : _WEBP_TAG_OFFSET + len(_WEBP_TAG)] == _WEBP_TAG
    ):
        return "image/webp"
    return None


def sniff_mime(data: bytes, filename: str | None = None) -> str | None:
    """Sniff a MIME type from ``data``'s leading bytes, with extension fallback.

    Magic-byte sniffing is authoritative. When no known magic matches, fall back
    to ``mimetypes.guess_type`` on ``filename``. Returns ``None`` when neither
    yields a type.
    """
    mime = _sniff_magic(data)
    if mime is not None:
        return mime
    if filename is not None:
        guessed, _ = mimetypes.guess_type(filename)
        return guessed
    return None


# ---------------------------------------------------------------------------
# Kind classification. Extractable-document MIME is a closed allow-list of the
# pdf/docx/xlsx/pptx families plus the text/* family.
# ---------------------------------------------------------------------------
_EXTRACTABLE_DOCUMENT_MIMES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        # Legacy MS Office binary formats — text-extractable too.
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        # Structured text documents commonly parsed as documents.
        "application/json",
        "application/xml",
        "text/xml",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/html",
    }
)


def classify_kind(mime: str | None) -> Kind:
    """Classify an authoritative MIME into the three-way :class:`Kind`.

    - ``image/*`` → :attr:`Kind.IMAGE`
    - the extractable-document MIME set (pdf/docx/xlsx/pptx + text family) →
      :attr:`Kind.EXTRACTABLE_DOCUMENT`
    - everything else (including ``None``) → :attr:`Kind.OTHER`
    """
    if mime is None:
        return Kind.OTHER
    if mime.startswith("image/"):
        return Kind.IMAGE
    if mime in _EXTRACTABLE_DOCUMENT_MIMES or mime.startswith("text/"):
        return Kind.EXTRACTABLE_DOCUMENT
    return Kind.OTHER


__all__: Sequence[str] = ("sniff_mime", "classify_kind")
