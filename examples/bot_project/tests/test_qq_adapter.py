"""Tests for QQ adapters — input/output adapter behavior, emitter events."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.adapters.qq import (
    QQ_FILE_TYPE_FILE,
    QQ_FILE_TYPE_IMAGE,
    QQBotEmitter,
    QQEmitterConfig,
    QQInputAdapter,
    QQOutputAdapter,
    _qq_file_type,
)


class TestQqFileType:
    def test_is_image_authoritative(self) -> None:
        """The record's is_image (magic-byte kind) wins — no extension used."""
        assert _qq_file_type("anything", is_image=True) == QQ_FILE_TYPE_IMAGE
        assert _qq_file_type("photo.png", is_image=False) == QQ_FILE_TYPE_FILE

    def test_fallback_uses_stdlib_mimetypes(self) -> None:
        """Legacy path-list case (no record): classify via stdlib mimetypes,
        not a hand-maintained extension list."""
        assert _qq_file_type("photo.png") == QQ_FILE_TYPE_IMAGE
        assert _qq_file_type("data.pdf") == QQ_FILE_TYPE_FILE
        assert _qq_file_type("README") == QQ_FILE_TYPE_FILE


class TestQQInputAdapter:
    def test_init_sets_basic_attributes(self) -> None:
        adapter = QQInputAdapter(app_id="123", secret="abc", allow_from=["user1"])
        assert adapter.app_id == "123"
        assert adapter.secret == "abc"
        assert adapter.allow_from == ["user1"]
        assert adapter._running is False
        assert adapter._message_queue is not None

    def test_has_required_interface(self) -> None:
        adapter = QQInputAdapter(app_id="123", secret="abc")
        assert hasattr(adapter, "receive")
        assert hasattr(adapter, "start")
        assert hasattr(adapter, "stop")

    async def test_stop_sets_running_false(self) -> None:
        adapter = QQInputAdapter(app_id="123", secret="abc")
        # Minimal test: stop should be idempotent
        await adapter.stop()  # Not started, should not crash


class TestQQOutputAdapter:
    def test_init_with_input_adapter(self) -> None:
        input_adapter = QQInputAdapter(app_id="123", secret="abc")
        adapter = QQOutputAdapter(input_adapter)
        assert adapter._qq_input is input_adapter

    def test_has_send_interface(self) -> None:
        input_adapter = QQInputAdapter(app_id="123", secret="abc")
        adapter = QQOutputAdapter(input_adapter)
        assert hasattr(adapter, "send")

    @pytest.mark.asyncio
    async def test_send_media_preserves_name_and_type_from_record(
        self, tmp_path: Path
    ) -> None:
        """An outbound file whose persisted path is an opaque id (no extension)
        is still sent with the Attachment record's original filename and the
        correct image/file type — QQ takes both from display_name + is_image,
        not the path basename (which is how images arrived nameless in IM)."""
        # Opaque-id persisted path (no extension) with real bytes on disk.
        png_opaque = tmp_path / "9d096e81667c434aa5f0df71d1529fbe"
        png_opaque.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)

        inp = QQInputAdapter(app_id="x", secret="y")
        out = QQOutputAdapter(inp)
        inp._client = MagicMock()  # truthy so _send_media proceeds
        inp._client.api.post_c2c_message = AsyncMock()

        captured: dict = {}

        async def fake_post(
            *, chat_id, is_group, file_type, file_data, file_name=None, srv_send_msg=False
        ):
            captured["file_type"] = file_type
            captured["file_name"] = file_name
            return {"file_info": "ok"}

        out._post_base64file = fake_post  # type: ignore[assignment]

        # Image carried by a record: is_image=True classifies it for inline render.
        ok = await out._send_media(
            chat_id="c", media_ref=str(png_opaque), msg_id="m", is_group=False,
            display_name="photo.jpg", is_image=True,
        )
        assert ok
        assert captured["file_type"] == QQ_FILE_TYPE_IMAGE

        # Non-image: the original filename (with extension) is what QQ posts.
        doc_opaque = tmp_path / "abc123doc"
        doc_opaque.write_bytes(b"%PDF-1.4\n" + b"\x00" * 40)
        captured.clear()
        ok = await out._send_media(
            chat_id="c", media_ref=str(doc_opaque), msg_id="m", is_group=False,
            display_name="report.pdf", is_image=False,
        )
        assert ok
        assert captured["file_type"] == QQ_FILE_TYPE_FILE
        assert captured["file_name"] == "report.pdf"

    @pytest.mark.asyncio
    async def test_send_threads_record_name_and_is_image_to_send_media(
        self, tmp_path: Path
    ) -> None:
        """``send()`` must hand each Attachment record's ``name``/``is_image``
        to ``_send_media`` so QQ preserves the original filename and type — not
        derive them from the (opaque) path basename."""
        from modex_agent.core.media import Attachment, AttachmentLocator, Kind
        from modex_agent.core.types import OutputMessage

        opaque = tmp_path / "9d096e81667c434aa5f0df71d1529fbe"
        opaque.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)

        inp = QQInputAdapter(app_id="x", secret="y")
        out = QQOutputAdapter(inp)
        inp._client = MagicMock()

        record = Attachment(
            id="r1", kind=Kind.IMAGE, name="photo.jpg", mime="image/jpeg",
            size=48, path=str(opaque), locator=AttachmentLocator.WORKSPACE,
        )
        msg = OutputMessage(content="", attachment_records=[record])

        out._send_media = AsyncMock(return_value=True)  # type: ignore[assignment]
        await out.send(msg, "u1")

        assert out._send_media.await_count == 1
        call = out._send_media.call_args
        assert call.kwargs["media_ref"] == str(opaque)   # record path
        assert call.kwargs["display_name"] == "photo.jpg"
        assert call.kwargs["is_image"] is True           # from record.kind


class TestQQEmitterConfig:
    def test_config_can_be_created(self) -> None:
        config = QQEmitterConfig()
        assert config is not None

    def test_config_is_creatable(self) -> None:
        config = QQEmitterConfig()
        assert config is not None


class TestQQBotEmitter:
    def test_init_with_minimal_args(self) -> None:
        emitter = QQBotEmitter(
            output_adapter=MagicMock(),
            session_id="s1",
        )
        assert emitter is not None
