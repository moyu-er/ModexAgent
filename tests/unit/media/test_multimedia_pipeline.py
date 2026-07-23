"""多媒体管线修复 UT 验证

覆盖:
- Bug A: _apply_runtime_context_prefix 多模态 content 支持
- P1-2: handler 失败后跳过文档提取
- MediaHandler 可插拔架构
- Error Fix 1: _is_transient 匹配 InternalServerError / empty response
- Error Fix 2: _build_tool_message 结果大小截断
- Error Fix 3: Base64SanitizeTransformer 字符串内 base64 检测
"""

from pathlib import Path

import pytest

from modex_agent.agents.react.message_builder import build_tool_message
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.media.media_utils import (
    ImageHandler,
    MediaBlock,
    MediaHandler,
    MediaProcessor,
)


# ---------------------------------------------------------------------------
# Bug A: _apply_runtime_context_prefix
# ---------------------------------------------------------------------------


class TestApplyRuntimeContextPrefix:
    """验证 _apply_runtime_context_prefix 对多模态 content 的处理。"""

    def _make_mgr(self) -> MemorySystemContextManager:
        """创建一个最小化的 MemorySystemContextManager 实例。"""
        mgr = object.__new__(MemorySystemContextManager)
        return mgr

    def test_string_content_unchanged(self):
        """纯文本 content 正常注入 prefix。"""
        mgr = self._make_mgr()
        msg = {"role": "user", "content": "hello"}
        result = mgr._apply_runtime_context_prefix(msg, {"chat_id": "123"})
        assert result["content"].startswith("[Runtime Context]")
        assert "hello" in result["content"]
        assert "chat_id=123" in result["content"]

    def test_multimodal_content_no_crash(self):
        """多模态 list content 不崩溃，prefix 作为 text block 插入头部。"""
        mgr = self._make_mgr()
        content = [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            {"type": "text", "text": "描述这张图"},
        ]
        msg = {"role": "user", "content": content}
        result = mgr._apply_runtime_context_prefix(msg, {"chat_id": "456"})
        assert isinstance(result["content"], list)
        assert result["content"][0].type == "text"
        assert "[Runtime Context]" in result["content"][0].text
        assert "chat_id=456" in result["content"][0].text
        assert result["content"][1].type == "image_url"
        assert result["content"][2].type == "text"
        assert result["content"][2].text == "描述这张图"

    def test_empty_list_returns_original(self):
        """空 list content 不注入 prefix，返回原对象。"""
        mgr = self._make_mgr()
        msg = {"role": "user", "content": []}
        result = mgr._apply_runtime_context_prefix(msg, {"chat_id": "123"})
        assert result is msg

    def test_no_metadata_returns_original(self):
        """无 metadata 时原样返回。"""
        mgr = self._make_mgr()
        msg = {"role": "user", "content": [{"type": "image_url", "image_url": {}}]}
        result = mgr._apply_runtime_context_prefix(msg, None)
        assert result is msg

    def test_no_runtime_lines_returns_original(self):
        """metadata 中无 channel/chat_id 时不注入 prefix。"""
        mgr = self._make_mgr()
        msg = {"role": "user", "content": "hello"}
        result = mgr._apply_runtime_context_prefix(msg, {"other_key": "value"})
        assert result is msg

    def test_none_content_treated_as_empty_string(self):
        """content 为 None 时使用 str(None) 处理。"""
        mgr = self._make_mgr()
        msg = {"role": "user", "content": None}
        result = mgr._apply_runtime_context_prefix(msg, {"chat_id": "123"})
        assert isinstance(result["content"], str)
        assert result["content"].startswith("[Runtime Context]")

    def test_channel_and_chat_id_both_present(self):
        """channel 和 chat_id 同时存在时都出现在 prefix 中。"""
        mgr = self._make_mgr()
        msg = {"role": "user", "content": "test"}
        result = mgr._apply_runtime_context_prefix(
            msg, {"channel": "qq", "chat_id": "789"}
        )
        assert "channel=qq" in result["content"]
        assert "chat_id=789" in result["content"]


# ---------------------------------------------------------------------------
# MediaHandler 可插拔架构
# ---------------------------------------------------------------------------


class TestMediaBlock:
    """验证 MediaBlock 数据类。"""

    def test_frozen_equality(self):
        b1 = MediaBlock(
            block={"type": "text", "text": "hi"},
            source_path="a.png",
            media_type="image",
        )
        b2 = MediaBlock(
            block={"type": "text", "text": "hi"},
            source_path="a.png",
            media_type="image",
        )
        assert b1 == b2

    def test_frozen_inequality(self):
        b1 = MediaBlock(
            block={"type": "text", "text": "hi"},
            source_path="a.png",
            media_type="image",
        )
        b2 = MediaBlock(
            block={"type": "text", "text": "other"},
            source_path="a.png",
            media_type="image",
        )
        assert b1 != b2

    def test_frozen_immutable(self):
        b = MediaBlock(
            block={"type": "image_url"},
            source_path="a.png",
            media_type="image",
        )
        with pytest.raises(AttributeError):
            b.media_type = "audio"


class TestImageHandler:
    """验证 ImageHandler 的 can_handle 逻辑。"""

    def test_can_handle_png(self):
        handler = ImageHandler()
        assert handler.can_handle("test.png")

    def test_can_handle_jpg(self):
        handler = ImageHandler()
        assert handler.can_handle("photo.JPG")

    def test_cannot_handle_txt(self):
        handler = ImageHandler()
        assert not handler.can_handle("readme.txt")

    def test_cannot_handle_pdf(self):
        handler = ImageHandler()
        assert not handler.can_handle("doc.pdf")

    @pytest.mark.asyncio
    async def test_encode_nonexistent_returns_none(self):
        handler = ImageHandler()
        result = await handler.encode("/nonexistent/path/image.png")
        assert result is None


class TestMediaProcessor:
    """验证 MediaProcessor 整体流程。"""

    @pytest.mark.asyncio
    async def test_process_empty_attachments(self):
        processor = MediaProcessor()
        result = await processor.process([])
        assert result.document_text == ""
        assert result.media_blocks == []
        assert result.att_meta == []

    @pytest.mark.asyncio
    async def test_process_nonexistent_file(self):
        processor = MediaProcessor()
        result = await processor.process(["/nonexistent/file.png"])
        assert len(result.att_meta) == 1
        assert result.att_meta[0]["exists"] is False
        assert "error" in result.att_meta[0]

    def test_build_content_no_blocks_returns_string(self):
        processor = MediaProcessor()
        result = processor.build_content("hello", [])
        assert result == "hello"

    def test_build_content_with_blocks_returns_list(self):
        processor = MediaProcessor()
        blocks = [
            MediaBlock(
                block={"type": "image_url", "image_url": {"url": "data:..."}},
                source_path="a.png",
                media_type="image",
            )
        ]
        result = processor.build_content("describe", blocks)
        assert isinstance(result, list)
        assert result[0]["type"] == "image_url"
        assert result[-1]["type"] == "text"
        assert result[-1]["text"] == "describe"


# ---------------------------------------------------------------------------
# P1-2: handler 失败后跳过文档提取
# ---------------------------------------------------------------------------


class TestHandlerFailureFallback:
    """验证 handler can_handle=True 但 encode=None 时跳过文档提取。"""

    @pytest.mark.asyncio
    async def test_corrupted_image_skips_doc_extraction(self):
        """模拟图片文件存在但编码失败（如文件损坏），不 fallthrough 到文档提取。"""

        class FailingImageHandler(MediaHandler):
            def can_handle(self, path: str) -> bool:
                return Path(path).suffix.lower() == ".png"

            async def encode(self, path: str) -> MediaBlock | None:
                return None  # 模拟编码失败

        processor = MediaProcessor()
        processor._handlers = [FailingImageHandler()]

        # 创建一个假的 .png 文件
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"not a real image")
            tmp_path = f.name

        try:
            result = await processor.process([tmp_path])
            # 应标记为 media_error，而非 document
            assert len(result.att_meta) == 1
            assert result.att_meta[0]["type"] == "media_error"
            assert "error" in result.att_meta[0]
            assert result.document_text == ""
            assert result.media_blocks == []
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_text_file_still_extracted(self):
        """非媒体文件（如 .txt）仍然走文档提取路径。"""
        import tempfile

        with tempfile.NamedTemporaryFile(
            suffix=".txt", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write("Hello world")
            tmp_path = f.name

        try:
            processor = MediaProcessor()
            result = await processor.process([tmp_path])
            assert len(result.att_meta) == 1
            assert result.att_meta[0]["type"] == "document"
            assert "Hello world" in result.document_text
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Error Fix 1: _is_transient
# ---------------------------------------------------------------------------


class TestIsTransient:
    """验证 _is_transient 能匹配 InternalServerError 和空响应。"""

    def test_internal_server_error(self):
        from modex_agent.core.provider import LLMProvider

        assert LLMProvider._is_transient(
            Exception("litellm.InternalServerError: Empty or invalid response")
        ) is True

    def test_empty_response(self):
        from modex_agent.core.provider import LLMProvider

        assert LLMProvider._is_transient(Exception("Empty response from API")) is True

    def test_invalid_response(self):
        from modex_agent.core.provider import LLMProvider

        assert LLMProvider._is_transient(Exception("invalid response from LLM endpoint")) is True

    def test_existing_markers_still_work(self):
        from modex_agent.core.provider import LLMProvider

        assert LLMProvider._is_transient(Exception("429 Too Many Requests")) is True
        assert LLMProvider._is_transient(Exception("502 Bad Gateway")) is True
        assert LLMProvider._is_transient(Exception("rate limit exceeded")) is True

    def test_non_transient_not_matched(self):
        from modex_agent.core.provider import LLMProvider

        assert LLMProvider._is_transient(Exception("invalid api key")) is False
        assert LLMProvider._is_transient(Exception("model not found")) is False

    def test_billing_error_not_retryable(self):
        from modex_agent.core.provider import LLMProvider

        assert LLMProvider._is_transient(
            Exception("500 server error insufficient_quota")
        ) is False


# ---------------------------------------------------------------------------
# Error Fix 2: _build_tool_message truncation
# ---------------------------------------------------------------------------


class TestBuildToolMessage:
    """验证 build_tool_message 不截断结果，正确传递 XML 元数据。"""

    def test_short_result_not_truncated(self):
        from modex_agent.core.tool_manager import ToolResult

        result = ToolResult(tool_name="test", result="short output")
        msg = build_tool_message(result)
        assert msg.content == "short output"

    def test_long_result_not_truncated(self):
        from modex_agent.core.tool_manager import ToolResult

        long_content = "x" * 30000
        result = ToolResult(tool_name="test", result=long_content)
        msg = build_tool_message(result)
        assert msg.content == long_content
        assert len(msg.content) == 30000

    def test_error_not_truncated(self):
        from modex_agent.core.tool_manager import ToolResult

        result = ToolResult(tool_name="test", error="something failed")
        msg = build_tool_message(result)
        assert msg.content == "Error: something failed"

    def test_empty_result_gets_space(self):
        from modex_agent.core.tool_manager import ToolResult

        result = ToolResult(tool_name="test", result=None)
        msg = build_tool_message(result)
        assert msg.content == " "

    def test_terminal_xml_sets_metadata(self):
        """build_tool_message passes through metadata declared on the ToolResult.

        Under ADR-0006 the ToolManager attaches content_format / truncatable_paths
        via the tool's result_metadata hook; build_tool_message no longer sniffs
        terminal XML itself. Terminal tool results arrive with metadata already set.
        """
        from modex_agent.core.tool_manager import ToolResult
        from modex_agent.core.message import ContentFormat

        xml_content = (
            "<command_result>"
            "<terminal>default</terminal>"
            "<output>hello</output>"
            "<status>completed</status>"
            "</command_result>"
        )
        result = ToolResult(
            tool_name="bash",
            result=xml_content,
            content_format=ContentFormat.XML,
            truncatable_paths=["output", "tui_screen", "cursor_line"],
        )
        msg = build_tool_message(result)
        assert msg.content_format == ContentFormat.XML
        assert msg.truncatable_paths == ["output", "tui_screen", "cursor_line"]

    def test_plain_text_no_metadata(self):
        from modex_agent.core.tool_manager import ToolResult
        from modex_agent.core.message import ContentFormat

        result = ToolResult(tool_name="grep", result="Found 3 matches")
        msg = build_tool_message(result)
        assert msg.content_format == ContentFormat.PLAIN
        assert msg.truncatable_paths is None


# ---------------------------------------------------------------------------
# Error Fix 3: Base64SanitizeTransformer string base64 detection
# ---------------------------------------------------------------------------


class TestBase64SanitizeStringDetection:
    """验证 Base64SanitizeTransformer 检测字符串中的内嵌 base64 data URI。"""

    @pytest.mark.asyncio
    async def test_string_with_base64_data_uri(self):
        from modex_agent.memory.content_transform import Base64SanitizeTransformer

        transformer = Base64SanitizeTransformer()
        msg = {
            "role": "tool",
            "content": "Here is the image: data:image/png;base64,iVBORw0KGgoAAAANSUhEUg== end",
        }
        result = await transformer.transform_message(msg)
        assert "[media: base64_data]" in result["content"]
        assert "data:image/png" not in result["content"]
        assert "Here is the image:" in result["content"]

    @pytest.mark.asyncio
    async def test_plain_string_unchanged(self):
        from modex_agent.memory.content_transform import Base64SanitizeTransformer

        transformer = Base64SanitizeTransformer()
        msg = {"role": "user", "content": "Hello world"}
        result = await transformer.transform_message(msg)
        assert result["content"] == "Hello world"

    @pytest.mark.asyncio
    async def test_multiple_base64_uris_in_string(self):
        from modex_agent.memory.content_transform import Base64SanitizeTransformer

        transformer = Base64SanitizeTransformer()
        msg = {
            "role": "tool",
            "content": "img1: data:image/png;base64,abc== img2: data:image/jpeg;base64,def==",
        }
        result = await transformer.transform_message(msg)
        assert result["content"].count("[media: base64_data]") == 2
