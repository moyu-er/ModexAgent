"""Tests for media.models — Attachment value object + enums."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.media.models import Attachment, AttachmentLocator, Kind


class TestKindEnum:
    def test_stable_string_values(self) -> None:
        assert Kind.IMAGE.value == "image"
        assert Kind.EXTRACTABLE_DOCUMENT.value == "extractable_document"
        assert Kind.OTHER.value == "other"


class TestAttachmentLocatorEnum:
    def test_stable_string_values(self) -> None:
        assert AttachmentLocator.MEDIA.value == "media"
        assert AttachmentLocator.WORKSPACE.value == "workspace"


class TestAttachment:
    def _make(self) -> Attachment:
        return Attachment(
            id="att-1",
            kind=Kind.IMAGE,
            name="cat.png",
            mime="image/png",
            size=12345,
            path="media/main/uploads/s1/cat.png",
            locator=AttachmentLocator.MEDIA,
        )

    def test_construction_round_trips_fields(self) -> None:
        att = self._make()
        assert att.id == "att-1"
        assert att.kind is Kind.IMAGE
        assert att.name == "cat.png"
        assert att.mime == "image/png"
        assert att.size == 12345
        assert att.path == "media/main/uploads/s1/cat.png"
        assert att.locator is AttachmentLocator.MEDIA

    def test_mime_can_be_none(self) -> None:
        att = Attachment(
            id="att-2",
            kind=Kind.OTHER,
            name="blob",
            mime=None,
            size=0,
            path="/abs/blob",
            locator=AttachmentLocator.WORKSPACE,
        )
        assert att.mime is None

    def test_is_image_reflects_kind(self) -> None:
        """is_image is the channel-rendering switch derived from kind (magic
        bytes), not the filename — so a nameless/opaque record still classifies
        correctly and channels need no extension lists."""
        assert self._make().is_image is True
        doc = Attachment(
            id="att-3", kind=Kind.EXTRACTABLE_DOCUMENT, name="r.pdf",
            mime="application/pdf", size=1, path="r.pdf", locator=AttachmentLocator.WORKSPACE,
        )
        assert doc.is_image is False
        other = Attachment(
            id="att-4", kind=Kind.OTHER, name="x.bin",
            mime=None, size=1, path="x.bin", locator=AttachmentLocator.WORKSPACE,
        )
        assert other.is_image is False

    def test_frozen(self) -> None:
        att = self._make()
        with pytest.raises(ValidationError):
            att.name = "tampered.png"  # type: ignore[misc]

    def test_equality_by_value(self) -> None:
        a = self._make()
        b = self._make()
        assert a == b
