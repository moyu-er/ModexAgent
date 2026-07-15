"""Tests for ContentTransformer and Base64SanitizeTransformer."""

from __future__ import annotations

import pytest

from modex_agent.memory.content_transform import (
    Base64SanitizeTransformer,
    CompositeTransformer,
    ContentTransformer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_image_block(url: str, path: str = "") -> dict:
    block: dict = {"type": "image_url", "image_url": {"url": url}}
    if path:
        block["_meta"] = {"path": path}
    return block


def make_text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def make_audio_block(url: str, path: str = "") -> dict:
    block: dict = {"type": "input_audio", "input_audio": {"url": url}}
    if path:
        block["_meta"] = {"path": path}
    return block


def make_file_block(url: str, path: str = "") -> dict:
    block: dict = {"type": "file", "file": {"url": url}}
    if path:
        block["_meta"] = {"path": path}
    return block


# ---------------------------------------------------------------------------
# Base64SanitizeTransformer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestBase64SanitizeTransformer:
    async def test_plain_text_message_unchanged(self):
        """纯文本消息不转换，返回浅拷贝（避免 CompositeTransformer 链中污染原始数据）。"""
        transformer = Base64SanitizeTransformer()
        msg = {"role": "user", "content": "Hello world"}
        result = await transformer.transform_message(msg)

        # P0-1 修复：不再返回原始引用，改为浅拷贝以避免链式 transformer 污染
        assert result is not msg
        assert result == msg
        assert result["content"] == "Hello world"

    async def test_single_base64_image(self):
        """单张 base64 图片 → 占位符 + media_info。"""
        transformer = Base64SanitizeTransformer()
        block = make_image_block(
            url="data:image/png;base64,iVBORw0KGgo=",
            path="/data/media/qq/img_123.png",
        )
        msg = {"role": "user", "content": [block]}
        result = await transformer.transform_message(msg)

        assert result["content"] == "[media: img_123.png]"
        media_info = result["metadata"]["media_info"]
        assert len(media_info) == 1
        assert media_info[0]["type"] == "image"
        assert media_info[0]["path"] == "/data/media/qq/img_123.png"
        assert media_info[0]["mime"] == "image/png"
        assert media_info[0]["placeholder"] == "[media: img_123.png]"

    async def test_multiple_images_and_text(self):
        """多图片 + 文本混合 content → 全部转为 text 后合并为字符串。"""
        transformer = Base64SanitizeTransformer()
        blocks = [
            make_text_block("Check this out:"),
            make_image_block(
                "data:image/jpeg;base64,/9j/4AAQ=",
                "/data/media/qq/photo.jpg",
            ),
            make_text_block("And this one too:"),
            make_image_block(
                "data:image/png;base64,iVBORw=",
                "/data/media/qq/screenshot.png",
            ),
        ]
        msg = {"role": "user", "content": blocks}
        result = await transformer.transform_message(msg)

        # 所有 block 都转为 text 后合并为单个字符串
        content = result["content"]
        assert isinstance(content, str)
        assert content == (
            "Check this out:\n"
            "[media: photo.jpg]\n"
            "And this one too:\n"
            "[media: screenshot.png]"
        )

        media_info = result["metadata"]["media_info"]
        assert len(media_info) == 2
        assert media_info[0]["type"] == "image"
        assert media_info[1]["type"] == "image"

    async def test_already_sanitized_message(self):
        """已 sanitize 过的消息（content 为字符串）不应报错。"""
        transformer = Base64SanitizeTransformer()
        msg = {
            "role": "user",
            "content": "[media: img.png]\n\nWhat's in this image?",
            "metadata": {"media_info": [{"type": "image", "path": "/tmp/img.png"}]},
        }
        result = await transformer.transform_message(msg)

        # P0-1 修复：字符串 content 返回浅拷贝而非原始引用
        assert result is not msg
        assert result == msg

    async def test_tool_result_with_base64_image(self):
        """tool result 中的 base64 图片也应被 sanitize。"""
        transformer = Base64SanitizeTransformer()
        msg = {
            "role": "tool",
            "content": [
                make_image_block(
                    "data:image/png;base64,iVBORw0KGgo=",
                    "/tmp/tool_output.png",
                ),
            ],
            "tool_call_id": "call_123",
        }
        result = await transformer.transform_message(msg)

        assert result["content"] == "[media: tool_output.png]"
        assert result["metadata"]["media_info"][0]["type"] == "image"

    async def test_image_without_meta_path(self):
        """无 _meta.path 的图片使用 [media: (unknown)] 回退。"""
        transformer = Base64SanitizeTransformer()
        block = make_image_block("data:image/png;base64,iVBORw0KGgo=")
        msg = {"role": "user", "content": [block]}
        result = await transformer.transform_message(msg)

        assert result["content"] == "[media: (unknown)]"
        assert result["metadata"]["media_info"][0]["path"] == ""

    async def test_http_url_image_preserved(self):
        """http(s) URL 图片不应被 sanitize，应保留原样。"""
        transformer = Base64SanitizeTransformer()
        blocks = [
            make_text_block("Look at this:"),
            make_image_block("https://example.com/image.png"),
            make_image_block(
                "data:image/png;base64,iVBORw0KGgo=",
                "/data/media/qq/local.png",
            ),
        ]
        msg = {"role": "user", "content": blocks}
        result = await transformer.transform_message(msg)

        content = result["content"]
        assert isinstance(content, list)
        assert content[0]["text"] == "Look at this:"
        # http URL 图片保留原样
        assert content[1] == blocks[1]
        # base64 图片被替换
        assert content[2]["text"] == "[media: local.png]"

        media_info = result["metadata"]["media_info"]
        assert len(media_info) == 1  # 只有 base64 图片产生 media_info
        assert media_info[0]["path"] == "/data/media/qq/local.png"

    async def test_audio_and_file_blocks(self):
        """input_audio 和 file 类型的 data: URI 也应被 sanitize → 合并为字符串。"""
        transformer = Base64SanitizeTransformer()
        blocks = [
            make_audio_block(
                "data:audio/wav;base64,UklGRiQ=",
                "/data/media/qq/voice.wav",
            ),
            make_file_block(
                "data:application/pdf;base64,JVBERi0=",
                "/data/media/qq/doc.pdf",
            ),
        ]
        msg = {"role": "user", "content": blocks}
        result = await transformer.transform_message(msg)

        content = result["content"]
        assert isinstance(content, str)
        assert content == "[media: voice.wav]\n[media: doc.pdf]"

        media_info = result["metadata"]["media_info"]
        assert media_info[0]["type"] == "input_audio"
        assert media_info[0]["mime"] == "audio/wav"
        assert media_info[1]["type"] == "file"
        assert media_info[1]["mime"] == "application/pdf"

    async def test_custom_placeholder_template(self):
        """占位符模板可配置。"""
        transformer = Base64SanitizeTransformer(
            placeholder_template="[img: {name}]"
        )
        block = make_image_block(
            "data:image/png;base64,iVBORw0KGgo=",
            "/data/media/qq/cat.png",
        )
        msg = {"role": "user", "content": [block]}
        result = await transformer.transform_message(msg)

        assert result["content"] == "[img: cat.png]"
        assert result["metadata"]["media_info"][0]["placeholder"] == "[img: cat.png]"

    async def test_does_not_mutate_original_message(self):
        """不应修改原始 message dict。"""
        transformer = Base64SanitizeTransformer()
        original_content = [
            make_image_block(
                "data:image/png;base64,iVBORw0KGgo=",
                "/tmp/test.png",
            ),
        ]
        msg = {"role": "user", "content": original_content}
        await transformer.transform_message(msg)

        # 原始消息未被修改
        assert msg["content"] is original_content
        assert msg["content"][0]["type"] == "image_url"
        assert "metadata" not in msg

    async def test_transform_messages_batch(self):
        """批量转换接口。"""
        transformer = Base64SanitizeTransformer()
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "user",
                "content": [
                    make_image_block(
                        "data:image/png;base64,iVBORw0KGgo=",
                        "/tmp/a.png",
                    ),
                ],
            },
        ]
        results = await transformer.transform_messages(messages)

        # P0-1 修复：纯文本返回浅拷贝而非原始引用
        assert results[0] is not messages[0]
        assert results[0] == messages[0]
        assert results[1]["content"] == "[media: a.png]"

    async def test_returns_copy_for_text_with_metadata(self):
        """纯文本消息返回浅拷贝而非原始引用，修改结果不应影响原始消息。"""
        transformer = Base64SanitizeTransformer()
        msg = {"role": "user", "content": "hello", "metadata": {"key": "value"}}
        result = await transformer.transform_message(msg)

        assert result is not msg
        assert result["content"] == msg["content"]
        # 修改返回结果不应影响原始消息
        result["metadata"] = {"new": "data"}
        assert msg["metadata"] == {"key": "value"}


# ---------------------------------------------------------------------------
# CompositeTransformer
# ---------------------------------------------------------------------------

class DoublingTransformer(ContentTransformer):
    """测试用：将 content 文本加倍。"""

    async def transform_message(self, message: dict) -> dict:
        import copy

        msg = copy.deepcopy(message)
        content = msg.get("content", "")
        if isinstance(content, str):
            msg["content"] = content + content
        return msg


class PrefixTransformer(ContentTransformer):
    """测试用：在 content 前加前缀。"""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    async def transform_message(self, message: dict) -> dict:
        import copy

        msg = copy.deepcopy(message)
        content = msg.get("content", "")
        if isinstance(content, str):
            msg["content"] = self._prefix + content
        return msg


@pytest.mark.asyncio
class TestCompositeTransformer:
    async def test_sequential_application(self):
        """按顺序依次应用 transformer。"""
        composite = CompositeTransformer([
            PrefixTransformer("[A] "),
            PrefixTransformer("[B] "),
        ])
        msg = {"role": "user", "content": "hello"}
        result = await composite.transform_message(msg)

        # 先 [A]，再 [B] → "[B] [A] hello"
        assert result["content"] == "[B] [A] hello"

    async def test_order_matters(self):
        """顺序不同结果不同。"""
        composite1 = CompositeTransformer([
            PrefixTransformer("1"),
            DoublingTransformer(),
        ])
        composite2 = CompositeTransformer([
            DoublingTransformer(),
            PrefixTransformer("1"),
        ])

        msg = {"role": "user", "content": "x"}

        # 先 prefix 再 double: "1x" → "1x1x"
        result1 = await composite1.transform_message(msg)
        assert result1["content"] == "1x1x"

        # 先 double 再 prefix: "xx" → "1xx"
        result2 = await composite2.transform_message(msg)
        assert result2["content"] == "1xx"

    async def test_empty_transformers(self):
        """空列表直接透传。"""
        composite = CompositeTransformer([])
        msg = {"role": "user", "content": "hello"}
        result = await composite.transform_message(msg)

        assert result["content"] == "hello"

    async def test_composite_with_sanitize(self):
        """CompositeTransformer 包含 Base64SanitizeTransformer。"""
        composite = CompositeTransformer([
            Base64SanitizeTransformer(),
            PrefixTransformer("[desc] "),
        ])
        block = make_image_block(
            "data:image/png;base64,iVBORw0KGgo=",
            "/tmp/img.png",
        )
        msg = {"role": "user", "content": [block]}
        result = await composite.transform_message(msg)

        # sanitize 后 content 是字符串 "[media: img.png]"
        # 然后 prefix → "[desc] [media: img.png]"
        assert result["content"] == "[desc] [media: img.png]"
        assert "media_info" in result.get("metadata", {})
