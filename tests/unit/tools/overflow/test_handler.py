from __future__ import annotations

import pytest
from pathlib import Path

from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.overflow.handler import ToolResultOverflowHandler
from framework.tools.overflow.local import LocalFileToolOverflowStore


@pytest.fixture
async def handler(tmp_path: Path) -> ToolResultOverflowHandler:
    store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50)
    await store.initialize()
    cleaner = OverflowCleaner(store)
    h = ToolResultOverflowHandler(store=store, cleaner=cleaner, max_chars=100)
    yield h
    await cleaner.stop()


class TestStoreOverflow:
    @pytest.mark.asyncio
    async def test_store_overflow_returns_truncation_notice(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        content = "x" * 250
        notice, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
        )

        assert "[TOOL_RESULT_TRUNCATED]" in notice
        assert "truncated" in notice.lower()
        assert ref.total_chars == 250
        assert ref.chunk_count == 5

    @pytest.mark.asyncio
    async def test_store_overflow_short_content(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        content = "short content"
        notice, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
        )

        assert "[TOOL_RESULT_TRUNCATED]" in notice
        assert ref.total_chars == 13
        assert ref.chunk_count == 1
