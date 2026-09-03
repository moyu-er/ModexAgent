from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.message import TextPart, ToolCall
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolResult
from modex_agent.interceptor.abc import ToolCallContext
from modex_agent.interceptor.builtin.result_limit import ToolResultLimitInterceptor
from modex_agent.memory.history import ListMessageHistory
from modex_agent.tools.overflow.cleaner import OverflowCleaner
from modex_agent.tools.overflow.handler import ToolResultOverflowHandler
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore


def _make_ctx() -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=MagicMock(),
        session=SessionInfo.from_str("test.agent"),
    )


def _make_call(tool_name: str = "read_file", call_id: str | None = "tc_1") -> ToolCallContext:
    return ToolCallContext(
        tool_call=ToolCall(tool_name=tool_name, arguments={}, call_id=call_id),
        tool_name=tool_name,
        arguments={},
        session_id="sess_1",
    )


class TestShortResultPassesThrough:
    @pytest.mark.asyncio
    async def test_short_result_passes_through(self) -> None:
        interceptor = ToolResultLimitInterceptor(overflow_handler=None, max_chars=100)
        result = ToolResult.from_text("read_file", "short")
        next_call = AsyncMock(return_value=result)

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out is result
        assert out.overflow_processed is False


class TestLongResultOverflows:
    @pytest.mark.asyncio
    async def test_long_result_overflows(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()
        cleaner = OverflowCleaner(store)
        handler = ToolResultOverflowHandler(store=store, cleaner=cleaner)

        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)
        long_content = "a" * 300
        result = ToolResult.from_text("read_file", long_content, call_id="tc_1")
        next_call = AsyncMock(return_value=result)

        ctx = _make_ctx()
        call = _make_call()
        try:
            out = await interceptor.around_tool_call(ctx, call, next_call)
            await cleaner.flush()
        finally:
            await cleaner.stop()

        assert out.overflow_processed is True
        mc = out.message_content()
        assert mc.startswith("a" * 5)
        assert "OUTPUT ELIDED: 288 chars" in mc
        assert "[Full output (300 chars total) saved to:" in mc
        assert not mc.startswith("<")
        full_path = tmp_path / "tool_overflow" / "test.agent" / "tc_1" / "full.txt"
        assert full_path.read_text(encoding="utf-8") == long_content


class TestKeptSetCoversStore:
    """A1 regression: the scheduled kept-set must cover the call BEFORE its
    entry lands on disk, so a clean pass firing between store and schedule
    cannot delete the just-stored entry.

    The probe simulates the raced clean INSIDE the gather await: if the
    interceptor stores first and schedules later, the flushed clean sees the
    on-disk entry absent from the kept-set and deletes it — full.txt then
    vanishes before the model can read it.
    """

    @pytest.mark.asyncio
    async def test_cleanup_fired_during_gather_keeps_just_stored_entry(
        self, tmp_path: Path
    ) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()
        cleaner = OverflowCleaner(store, merge_window=0.01)
        handler = ToolResultOverflowHandler(store=store, cleaner=cleaner)

        async def _flush_clean_during_gather() -> set[str]:
            await cleaner.flush()
            return set()

        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)
        interceptor._gather_kept_call_ids = _flush_clean_during_gather  # type: ignore[method-assign]
        result = ToolResult.from_text("read_file", "a" * 300, call_id="tc_1")
        next_call = AsyncMock(return_value=result)

        try:
            out = await interceptor.around_tool_call(_make_ctx(), _make_call(), next_call)
            await cleaner.flush()
        finally:
            await cleaner.stop()

        assert out.overflow_processed is True
        full_path = tmp_path / "tool_overflow" / "test.agent" / "tc_1" / "full.txt"
        assert full_path.read_text(encoding="utf-8") == "a" * 300


class TestRebuildPreservesFields:
    """A3 regression: rebuilding after truncation keeps content_format /
    truncatable_paths and non-text content parts."""

    @pytest.mark.asyncio
    async def test_overflow_preserves_xml_metadata_and_image_part(
        self, tmp_path: Path
    ) -> None:
        from modex_agent.core.message import ContentFormat, ImageUrl, ImageUrlPart

        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()
        cleaner = OverflowCleaner(store)
        handler = ToolResultOverflowHandler(store=store, cleaner=cleaner)
        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)

        image = ImageUrlPart(image_url=ImageUrl(url="media://att-1"))
        result = ToolResult(
            tool_name="bash",
            call_id="tc_1",
            content_format=ContentFormat.XML,
            truncatable_paths=["output"],
            content=[TextPart(text="x" * 300), image],
        )
        next_call = AsyncMock(return_value=result)

        try:
            out = await interceptor.around_tool_call(_make_ctx(), _make_call(), next_call)
            await cleaner.flush()
        finally:
            await cleaner.stop()

        assert out.overflow_processed is True
        assert out.content_format is ContentFormat.XML
        assert out.truncatable_paths == ["output"]
        assert image in out.content
        text_parts = [p for p in out.content if isinstance(p, TextPart)]
        assert len(text_parts) == 1
        assert "OUTPUT ELIDED: 288 chars" in text_parts[0].text
        message = out.to_message()
        assert message["content_format"] == "xml"
        assert message["truncatable_paths"] == ["output"]


class TestErrorResultsAreBounded:
    @pytest.mark.asyncio
    async def test_oversized_error_result_truncated_without_handler(self) -> None:
        interceptor = ToolResultLimitInterceptor(overflow_handler=None, max_chars=50)
        result = ToolResult.from_text("bash", "E" * 300, call_id="tc_1", error="command failed")
        next_call = AsyncMock(return_value=result)

        out = await interceptor.around_tool_call(_make_ctx(), _make_call(), next_call)

        assert out.overflow_processed is False
        assert out.error == "command failed"
        mc = out.message_content()
        assert mc.startswith("E" * 5)
        assert "OUTPUT ELIDED: 288 chars" in mc
        assert "NOT saved" in mc

    @pytest.mark.asyncio
    async def test_oversized_error_result_overflows_through_handler(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()
        cleaner = OverflowCleaner(store)
        handler = ToolResultOverflowHandler(store=store, cleaner=cleaner)
        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)

        result = ToolResult.from_text("bash", "E" * 300, call_id="tc_1", error="command failed")
        next_call = AsyncMock(return_value=result)

        try:
            out = await interceptor.around_tool_call(_make_ctx(), _make_call(), next_call)
            await cleaner.flush()
        finally:
            await cleaner.stop()

        assert out.overflow_processed is True
        assert out.error == "command failed"
        mc = out.message_content()
        assert mc.startswith("E" * 5)
        assert "OUTPUT ELIDED: 288 chars" in mc
        assert "[Full output (300 chars total) saved to:" in mc
        full_path = tmp_path / "tool_overflow" / "test.agent" / "tc_1" / "full.txt"
        assert full_path.read_text(encoding="utf-8") == "E" * 300

    @pytest.mark.asyncio
    async def test_short_error_result_passes_through(self) -> None:
        interceptor = ToolResultLimitInterceptor(overflow_handler=None, max_chars=100)
        result = ToolResult.from_text("bash", "boom", call_id="tc_1", error="failed")
        next_call = AsyncMock(return_value=result)

        out = await interceptor.around_tool_call(_make_ctx(), _make_call(), next_call)

        assert out is result
        assert out.error == "failed"


class TestAlreadyProcessedSkips:
    @pytest.mark.asyncio
    async def test_already_processed_skips(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        cleaner = OverflowCleaner(store)
        handler = ToolResultOverflowHandler(store=store, cleaner=cleaner)
        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)
        long_content = "a" * 100
        result = ToolResult.from_text(
            "read_file",
            long_content,
            call_id="tc_1",
            overflow_processed=True,
        )
        next_call = AsyncMock(return_value=result)

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out is result
        assert not (tmp_path / "tool_overflow").exists()


class TestFallbackTruncationWhenNoHandler:
    @pytest.mark.asyncio
    async def test_fallback_truncation_when_no_handler(self) -> None:
        interceptor = ToolResultLimitInterceptor(overflow_handler=None, max_chars=50)
        long_content = "a" * 300
        result = ToolResult.from_text("read_file", long_content, call_id="tc_1")
        next_call = AsyncMock(return_value=result)

        ctx = _make_ctx()
        call = _make_call()
        out = await interceptor.around_tool_call(ctx, call, next_call)

        assert out.overflow_processed is False
        mc = out.message_content()
        # head 5 / tail 7 of max_chars=50, 288 chars elided, no path claim
        lines = mc.split("\n")
        assert len(lines) == 7
        assert lines[0] == "a" * 5
        assert lines[4] == "a" * 7
        assert "OUTPUT ELIDED: 288 chars" in lines[2]
        assert "NOT saved (overflow handler unavailable)" in lines[2]
        assert lines[6].startswith("[Full output (300 chars total) NOT saved to disk")
        assert "full.txt" not in mc


class TestHandlerFailureFallback:
    @pytest.mark.asyncio
    async def test_store_failure_falls_back_to_unpersisted_truncation(self) -> None:
        handler = MagicMock(spec=ToolResultOverflowHandler)
        handler.store_overflow = AsyncMock(side_effect=RuntimeError("disk full"))
        interceptor = ToolResultLimitInterceptor(overflow_handler=handler, max_chars=50)

        result = ToolResult.from_text("read_file", "a" * 300, call_id="tc_1", error="boom")
        next_call = AsyncMock(return_value=result)

        out = await interceptor.around_tool_call(_make_ctx(), _make_call(), next_call)

        assert out.overflow_processed is False
        assert out.error == "boom"
        mc = out.message_content()
        assert mc.startswith("a" * 5)
        assert "OUTPUT ELIDED: 288 chars" in mc
        assert "NOT saved" in mc
        assert "full.txt" not in mc
