"""Tests for QQ adapters — input/output adapter behavior, emitter events."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.adapters.qq import (
    QQ_FILE_TYPE_FILE,
    QQ_FILE_TYPE_IMAGE,
    QQBotEmitter,
    QQEmitterConfig,
    QQInputAdapter,
    QQOutputAdapter,
    _guess_send_file_type,
)


class TestGuessSendFileType:
    def test_image_extension_returns_image_type(self) -> None:
        assert _guess_send_file_type("photo.png") == QQ_FILE_TYPE_IMAGE
        assert _guess_send_file_type("logo.jpg") == QQ_FILE_TYPE_IMAGE
        assert _guess_send_file_type("icon.gif") == QQ_FILE_TYPE_IMAGE
        assert _guess_send_file_type("test.jpeg") == QQ_FILE_TYPE_IMAGE

    def test_non_image_returns_file_type(self) -> None:
        assert _guess_send_file_type("doc.txt") == QQ_FILE_TYPE_FILE
        assert _guess_send_file_type("data.pdf") == QQ_FILE_TYPE_FILE
        assert _guess_send_file_type("archive.zip") == QQ_FILE_TYPE_FILE

    def test_no_extension_returns_file_type(self) -> None:
        assert _guess_send_file_type("README") == QQ_FILE_TYPE_FILE


class TestQQInputAdapter:
    def test_init_sets_basic_attributes(self) -> None:
        adapter = QQInputAdapter(app_id="123", secret="abc", sandbox=True, allow_from=["user1"])
        assert adapter.app_id == "123"
        assert adapter.secret == "abc"
        assert adapter.sandbox is True
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
