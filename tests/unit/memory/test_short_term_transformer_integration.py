"""Integration tests: ShortTermMemoryManager + Base64SanitizeTransformer end-to-end."""

from __future__ import annotations

import json
import tempfile

import pytest

from framework.memory.content_transform import Base64SanitizeTransformer
from framework.memory.core.scope import SessionScope
from framework.memory.managers.short_term import ShortTermConfig, ShortTermMemoryManager
from framework.memory.stores.in_memory import InMemoryStorage


@pytest.fixture
def temp_storage():
    """提供临时目录的 FileStorage（用于需要持久化的测试）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        from framework.memory.stores.file import FileStorage
        from pathlib import Path

        store = FileStorage(Path(tmpdir))
        yield store


@pytest.fixture
def inmem_storage():
    return InMemoryStorage()


class TestShortTermWithSanitizeTransformer:
    """ShortTermMemoryManager 配置 Base64SanitizeTransformer 后的端到端测试。"""

    @pytest.mark.asyncio
    async def test_base64_image_sanitized_on_write(self, inmem_storage):
        """写入 base64 图片消息，读取时应为占位符 + media_info。"""
        config = ShortTermConfig(
            max_messages=100,
            content_transformer=Base64SanitizeTransformer(),
        )
        scope = SessionScope()
        mgr = ShortTermMemoryManager(inmem_storage, scope, config=config)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="test_session")

        msg = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,iVBORw0KGgo="  # 假的 base64
                    },
                    "_meta": {"path": "/data/media/qq/img_123.png"},
                }
            ],
        }

        await mgr.add_message(ctx, msg)
        messages = await mgr.get_messages(ctx)

        assert len(messages) == 1
        # content 应为占位符字符串
        assert messages[0]["content"] == "[media: img_123.png]"
        # metadata.media_info 应保留
        media_info = messages[0]["metadata"]["media_info"]
        assert len(media_info) == 1
        assert media_info[0]["type"] == "image"
        assert media_info[0]["path"] == "/data/media/qq/img_123.png"
        assert media_info[0]["mime"] == "image/png"

    @pytest.mark.asyncio
    async def test_plain_text_not_transformed(self, inmem_storage):
        """纯文本消息不应被转换。"""
        config = ShortTermConfig(
            content_transformer=Base64SanitizeTransformer(),
        )
        scope = SessionScope()
        mgr = ShortTermMemoryManager(inmem_storage, scope, config=config)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="test_session")
        msg = {"role": "user", "content": "Hello world"}

        await mgr.add_message(ctx, msg)
        messages = await mgr.get_messages(ctx)

        assert messages[0]["content"] == "Hello world"
        assert "metadata" not in messages[0]

    @pytest.mark.asyncio
    async def test_tool_result_with_base64_sanitized(self, inmem_storage):
        """tool result 中的 base64 也应被 sanitize。"""
        config = ShortTermConfig(
            content_transformer=Base64SanitizeTransformer(),
        )
        scope = SessionScope()
        mgr = ShortTermMemoryManager(inmem_storage, scope, config=config)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="test_session")
        msg = {
            "role": "tool",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64,/9j/4AAQ="
                    },
                    "_meta": {"path": "/tmp/screenshot.jpg"},
                }
            ],
            "tool_call_id": "call_123",
            "name": "screenshot_tool",
        }

        await mgr.add_message(ctx, msg)
        messages = await mgr.get_messages(ctx)

        assert messages[0]["content"] == "[media: screenshot.jpg]"
        assert messages[0]["metadata"]["media_info"][0]["type"] == "image"

    @pytest.mark.asyncio
    async def test_multimodal_mixed_content(self, inmem_storage):
        """文本 + 图片混合 content 应正确合并为字符串。"""
        config = ShortTermConfig(
            content_transformer=Base64SanitizeTransformer(),
        )
        scope = SessionScope()
        mgr = ShortTermMemoryManager(inmem_storage, scope, config=config)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="test_session")
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Look at this:"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,iVBORw0KGgo="
                    },
                    "_meta": {"path": "/data/photo.png"},
                },
            ],
        }

        await mgr.add_message(ctx, msg)
        messages = await mgr.get_messages(ctx)

        assert messages[0]["content"] == "Look at this:\n[media: photo.png]"

    @pytest.mark.asyncio
    async def test_file_storage_roundtrip(self, temp_storage):
        """FileStorage 持久化后，读取回来的消息应保持 sanitize 后的状态。"""
        config = ShortTermConfig(
            content_transformer=Base64SanitizeTransformer(),
        )
        scope = SessionScope()
        mgr = ShortTermMemoryManager(temp_storage, scope, config=config)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="test_session")
        msg = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,iVBORw0KGgo="
                    },
                    "_meta": {"path": "/data/img.png"},
                }
            ],
        }

        await mgr.add_message(ctx, msg)

        # 重新创建 manager（模拟重启），从 storage 读取
        mgr2 = ShortTermMemoryManager(temp_storage, scope, config=config)
        messages = await mgr2.get_messages(ctx)

        assert len(messages) == 1
        assert messages[0]["content"] == "[media: img.png]"
        assert messages[0]["metadata"]["media_info"][0]["path"] == "/data/img.png"

    @pytest.mark.asyncio
    async def test_no_transformer_when_none_configured(self, inmem_storage):
        """未配置 transformer 时，base64 应原样保存。"""
        config = ShortTermConfig(
            content_transformer=None,
        )
        scope = SessionScope()
        mgr = ShortTermMemoryManager(inmem_storage, scope, config=config)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="test_session")
        original_content = [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
            }
        ]
        msg = {"role": "user", "content": original_content}

        await mgr.add_message(ctx, msg)
        messages = await mgr.get_messages(ctx)

        # base64 应原样保留
        assert messages[0]["content"] == original_content
        assert "metadata" not in messages[0]

    @pytest.mark.asyncio
    async def test_audio_and_file_blocks_sanitized(self, inmem_storage):
        """input_audio 和 file 类型的 data: URI 也应被 sanitize。"""
        config = ShortTermConfig(
            content_transformer=Base64SanitizeTransformer(),
        )
        scope = SessionScope()
        mgr = ShortTermMemoryManager(inmem_storage, scope, config=config)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="test_session")
        msg = {
            "role": "user",
            "content": [
                {
                    "type": "input_audio",
                    "input_audio": {
                        "url": "data:audio/wav;base64,UklGRiQ="
                    },
                    "_meta": {"path": "/data/voice.wav"},
                },
                {
                    "type": "file",
                    "file": {"url": "data:application/pdf;base64,JVBERi0="},
                    "_meta": {"path": "/data/doc.pdf"},
                },
            ],
        }

        await mgr.add_message(ctx, msg)
        messages = await mgr.get_messages(ctx)

        assert messages[0]["content"] == "[media: voice.wav]\n[media: doc.pdf]"
        media_info = messages[0]["metadata"]["media_info"]
        assert media_info[0]["type"] == "input_audio"
        assert media_info[0]["mime"] == "audio/wav"
        assert media_info[1]["type"] == "file"
        assert media_info[1]["mime"] == "application/pdf"
