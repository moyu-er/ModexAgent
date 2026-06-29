"""Tests for media.gate — the one perception gate (ADR-0013 §7/§8).

The gate is the single authority for accept/reject shared by upload-accept,
path-injection, and inline-render. Ordering is load-bearing: disguise
(dangerous-magic) rejection runs BEFORE type/size so a PE disguised as .png is
rejected as dangerous regardless of the allowlisted extension.
"""

from __future__ import annotations

from modex_agent.ioc.configs.pool import MediaConfig
from modex_agent.media.gate import GateDecision, RejectReason, perception_gate
from modex_agent.media.models import Kind

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MZ = b"MZ"  # PE executable magic
_ZIP_MAGIC = b"PK\x03\x04"


def _png_bytes(size: int) -> bytes:
    """Build a PNG-magic-prefixed byte string of total length ``size``."""
    body = b"\x00" * (size - len(_PNG_MAGIC))
    return _PNG_MAGIC + body


def _png_head() -> bytes:
    return _PNG_MAGIC + b"\x00" * 8


class TestAccept:
    def test_5mb_png_accepts_as_image(self) -> None:
        size = 5 * 1024 * 1024
        decision = perception_gate(
            head=_png_head(), size=size, filename="photo.png", config=MediaConfig()
        )
        assert decision.accepted is True
        assert decision.reason is None
        assert decision.mime == "image/png"
        assert decision.kind is Kind.IMAGE

    def test_image_at_exactly_cap_accepts(self) -> None:
        config = MediaConfig()
        decision = perception_gate(
            head=_png_head(),
            size=config.max_image_bytes,
            filename="edge.png",
            config=config,
        )
        assert decision.accepted is True
        assert decision.kind is Kind.IMAGE

    def test_extractable_document_accepts(self) -> None:
        # PDF magic.
        head = b"%PDF-1.4\n" + b"\x00" * 8
        decision = perception_gate(
            head=head, size=1024, filename="doc.pdf", config=MediaConfig()
        )
        assert decision.accepted is True
        assert decision.kind is Kind.EXTRACTABLE_DOCUMENT


class TestReject:
    def test_image_one_byte_over_cap_rejects_oversize(self) -> None:
        config = MediaConfig()
        decision = perception_gate(
            head=_png_head(),
            size=config.max_image_bytes + 1,
            filename="over.png",
            config=config,
        )
        assert decision.accepted is False
        assert decision.reason is RejectReason.OVERSIZE
        assert decision.kind is Kind.IMAGE  # classified even when rejected

    def test_oversize_text_document_rejects(self) -> None:
        # 50 MB text/plain — over the 10 MB text-doc cap. ``.txt`` resolves to
        # text/plain via extension fallback, classifying as EXTRACTABLE_DOCUMENT
        # so the file reaches the per-kind size cap (not the type allow-list).
        head = b"hello world\n" + b"\x00" * 8
        decision = perception_gate(
            head=head,
            size=50 * 1024 * 1024,
            filename="big.txt",
            config=MediaConfig(),
        )
        assert decision.accepted is False
        assert decision.reason is RejectReason.OVERSIZE
        assert decision.kind is Kind.EXTRACTABLE_DOCUMENT

    def test_non_allowlisted_kind_rejects_type_not_allowed(self) -> None:
        # ZIP is not image and not an extractable document.
        head = _ZIP_MAGIC + b"\x00" * 16
        decision = perception_gate(
            head=head, size=512, filename="archive.zip", config=MediaConfig()
        )
        assert decision.accepted is False
        assert decision.reason is RejectReason.TYPE_NOT_ALLOWED
        assert decision.kind is Kind.OTHER

    def test_pe_disguised_as_png_rejects_dangerous_disguise(self) -> None:
        """A PE executable whose bytes start with MZ must be rejected even
        though the extension ``.png`` is allowlisted — disguise-evasion is the
        exact attack the dangerous-magic check defends against (ADR §8)."""
        head = _MZ + b"\x00" * 30
        decision = perception_gate(
            head=head, size=4096, filename="trojan.png", config=MediaConfig()
        )
        assert decision.accepted is False
        assert decision.reason is RejectReason.DANGEROUS_DISGUISE

    def test_dangerous_check_beats_type_allowlist(self) -> None:
        """The disguise check must reject BEFORE the type check could accept.

        ``MZ`` does not sniff to image/png by magic (only the PNG magic does),
        so this also asserts the gate does not fall back to the .png extension
        and let a PE through as an image.
        """
        head = _MZ + b"\x89PNG\r\n\x1a\n"  # MZ prefix, PNG magic later — still PE
        decision = perception_gate(
            head=head, size=100, filename="tricky.png", config=MediaConfig()
        )
        assert decision.accepted is False
        assert decision.reason is RejectReason.DANGEROUS_DISGUISE


class TestGateDecisionValueObject:
    def test_accepted_decision_carries_mime_and_kind(self) -> None:
        d = GateDecision(accepted=True, mime="image/png", kind=Kind.IMAGE, reason=None)
        assert d.accepted is True
        assert d.reason is None

    def test_rejected_decision_carries_reason_not_mime(self) -> None:
        d = GateDecision(
            accepted=False, mime=None, kind=Kind.OTHER, reason=RejectReason.OVERSIZE
        )
        assert d.accepted is False
        assert d.reason is RejectReason.OVERSIZE
