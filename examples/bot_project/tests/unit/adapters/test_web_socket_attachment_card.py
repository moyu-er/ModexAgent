"""Tests for the outbound attachment-card delta (G8 §8.1).

Verifies :meth:`WebSocketOutputAdapter.send` emits one ``attachment_card``
``DeltaEnvelope`` per outbound Attachment record, carrying the renderer-facing
fields (kind, name, size, mime, download_url). Direction-agnostic: the adapter
describes the attachment; the frontend picks inline-image / file-card /
fallback.
"""

from __future__ import annotations

import pytest
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.events import WebUIEventType

from modex_agent.core.media import Attachment, AttachmentLocator, Kind
from modex_agent.messaging.models import OutputMessage


def _record(kind: Kind, *, name: str = "f.png", mime: str | None = "image/png",
            size: int = 123, attachment_id: str = "att1") -> Attachment:
    return Attachment(
        id=attachment_id,
        kind=kind,
        name=name,
        mime=mime,
        size=size,
        path="/tmp/f",
        locator=AttachmentLocator.WORKSPACE,
    )


@pytest.mark.asyncio
async def test_image_attachment_emits_card_with_image_kind_and_download_url() -> None:
    """An image record produces an attachment_card delta with kind=image and a
    download URL pointing at the G6 endpoint."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("sess1", None)

    record = _record(Kind.IMAGE)
    await output_adapter.send(
        OutputMessage(content="", attachment_records=[record]),
        "sess1",
    )

    q = input_adapter.get_delta_queue("sess1", None)
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == WebUIEventType.ATTACHMENT_CARD.value
    assert envelope.payload["kind"] == "image"
    assert envelope.payload["name"] == "f.png"
    assert envelope.payload["size"] == 123
    assert envelope.payload["mime"] == "image/png"
    assert envelope.payload["attachment_id"] == "att1"
    assert envelope.payload["download_url"] == (
        "/api/sessions/sess1/attachments/att1"
    )


@pytest.mark.asyncio
async def test_non_image_attachment_emits_file_kind() -> None:
    """A non-image record (extractable doc / OTHER) produces kind=file."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("sess1", None)

    record = _record(Kind.EXTRACTABLE_DOCUMENT, name="report.pdf",
                     mime="application/pdf", attachment_id="att2")
    await output_adapter.send(
        OutputMessage(content="", attachment_records=[record]),
        "sess1",
    )

    q = input_adapter.get_delta_queue("sess1", None)
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == WebUIEventType.ATTACHMENT_CARD.value
    assert envelope.payload["kind"] == "file"
    assert envelope.payload["name"] == "report.pdf"
    assert envelope.payload["download_url"] == (
        "/api/sessions/sess1/attachments/att2"
    )


@pytest.mark.asyncio
async def test_other_kind_also_file_card() -> None:
    """Kind.OTHER renders as a file card (only images go inline)."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("sess1", None)

    record = _record(Kind.OTHER, name="data.bin", mime="application/octet-stream")
    await output_adapter.send(
        OutputMessage(content="", attachment_records=[record]), "sess1"
    )
    q = input_adapter.get_delta_queue("sess1", None)
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.payload["kind"] == "file"


@pytest.mark.asyncio
async def test_accompanying_text_sent_before_card() -> None:
    """When the OutputMessage has both content and an attachment, the text is
    delivered first (as a content delta), then the card — so the card renders
    after the accompanying message."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("sess1", None)

    record = _record(Kind.IMAGE)
    await output_adapter.send(
        OutputMessage(content="here is the chart", attachment_records=[record]),
        "sess1",
    )
    q = input_adapter.get_delta_queue("sess1", None)
    assert q is not None
    first = q.get_nowait()
    assert first.event_type == "content"
    assert first.payload == {"text": "here is the chart"}
    second = q.get_nowait()
    assert second.event_type == WebUIEventType.ATTACHMENT_CARD.value


@pytest.mark.asyncio
async def test_multiple_records_emit_one_card_each() -> None:
    """Multiple attachments produce one card delta per record, in order."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("sess1", None)

    r1 = _record(Kind.IMAGE, name="a.png", attachment_id="a")
    r2 = _record(Kind.OTHER, name="b.bin", attachment_id="b")
    await output_adapter.send(
        OutputMessage(content="", attachment_records=[r1, r2]), "sess1"
    )
    q = input_adapter.get_delta_queue("sess1", None)
    assert q is not None
    e1 = q.get_nowait()
    e2 = q.get_nowait()
    assert e1.payload["attachment_id"] == "a"
    assert e2.payload["attachment_id"] == "b"


@pytest.mark.asyncio
async def test_no_records_falls_back_to_content_delta() -> None:
    """An OutputMessage without attachment_records behaves as before: a single
    content delta. No attachment_card is emitted."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("sess1", None)

    await output_adapter.send(OutputMessage(content="plain text"), "sess1")
    q = input_adapter.get_delta_queue("sess1", None)
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == "content"
    assert envelope.payload == {"text": "plain text"}
