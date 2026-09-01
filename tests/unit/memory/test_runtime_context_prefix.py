"""Tests for ``MemorySystemContextManager._apply_runtime_context_prefix``.

Migrated from tests/unit/media/test_multimedia_pipeline.py (2026-09) when the
dormant MediaProcessor family was removed — these tests cover the live
multimodal-content prefix injection of the memory system, which merely lived
in the same legacy file.
"""

from __future__ import annotations

from modex_agent.memory.system import MemorySystemContextManager


def _make_mgr() -> MemorySystemContextManager:
    return object.__new__(MemorySystemContextManager)  # noqa: SLF001


class TestApplyRuntimeContextPrefix:
    def test_string_content_unchanged(self):
        mgr = _make_mgr()
        msg = {"role": "user", "content": "hello"}
        result = mgr._apply_runtime_context_prefix(msg, {"chat_id": "123"})  # noqa: SLF001
        assert result["content"].startswith("[Runtime Context]")
        assert "hello" in result["content"]
        assert "chat_id=123" in result["content"]

    def test_multimodal_content_no_crash(self):
        mgr = _make_mgr()
        content = [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            {"type": "text", "text": "描述这张图"},
        ]
        msg = {"role": "user", "content": content}
        result = mgr._apply_runtime_context_prefix(msg, {"chat_id": "456"})  # noqa: SLF001
        assert isinstance(result["content"], list)
        assert result["content"][0].type == "text"
        assert "[Runtime Context]" in result["content"][0].text
        assert "chat_id=456" in result["content"][0].text
        assert result["content"][1].type == "image_url"
        assert result["content"][2].type == "text"
        assert result["content"][2].text == "描述这张图"

    def test_empty_list_returns_original(self):
        mgr = _make_mgr()
        msg = {"role": "user", "content": []}
        result = mgr._apply_runtime_context_prefix(msg, {"chat_id": "123"})  # noqa: SLF001
        assert result is msg

    def test_no_metadata_returns_original(self):
        mgr = _make_mgr()
        msg = {"role": "user", "content": [{"type": "image_url", "image_url": {}}]}
        result = mgr._apply_runtime_context_prefix(msg, None)  # noqa: SLF001
        assert result is msg

    def test_no_runtime_lines_returns_original(self):
        mgr = _make_mgr()
        msg = {"role": "user", "content": "hello"}
        result = mgr._apply_runtime_context_prefix(msg, {"other_key": "value"})  # noqa: SLF001
        assert result is msg

    def test_none_content_treated_as_empty_string(self):
        mgr = _make_mgr()
        msg = {"role": "user", "content": None}
        result = mgr._apply_runtime_context_prefix(msg, {"chat_id": "123"})  # noqa: SLF001
        assert isinstance(result["content"], str)
        assert result["content"].startswith("[Runtime Context]")

    def test_channel_and_chat_id_both_present(self):
        mgr = _make_mgr()
        msg = {"role": "user", "content": "test"}
        result = mgr._apply_runtime_context_prefix(  # noqa: SLF001
            msg, {"channel": "qq", "chat_id": "789"}
        )
        assert "channel=qq" in result["content"]
        assert "chat_id=789" in result["content"]
