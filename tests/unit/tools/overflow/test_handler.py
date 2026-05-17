from __future__ import annotations

import pytest
from pathlib import Path

from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.overflow.handler import ToolResultOverflowHandler
from framework.tools.overflow.local import LocalFileToolOverflowStore


@pytest.fixture
async def handler(tmp_path: Path) -> ToolResultOverflowHandler:
    store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
    await store.initialize()
    cleaner = OverflowCleaner(store)
    await cleaner.start()
    h = ToolResultOverflowHandler(store=store, cleaner=cleaner, max_chars=100, summary_chars=20)
    yield h
    await cleaner.stop()


class TestMaxChunkSizeFormula:
    def test_max_chunk_size_formula(self) -> None:
        store = LocalFileToolOverflowStore(workspace=Path("/tmp"), max_chunk_size=50, summary_chars=20)
        cleaner = OverflowCleaner(store)
        handler = ToolResultOverflowHandler(store=store, cleaner=cleaner, max_chars=1000)
        # max(0.9 * 1000, 1000 - 200) = max(900, 800) = 900
        assert handler.max_chunk_size == 900

        handler2 = ToolResultOverflowHandler(store=store, cleaner=cleaner, max_chars=100)
        # max(0.9 * 100, 100 - 200) = max(90, -100) = 90
        assert handler2.max_chunk_size == 90


class TestMakePrefix:
    def test_make_prefix(self) -> None:
        store = LocalFileToolOverflowStore(workspace=Path("/tmp"), max_chunk_size=50, summary_chars=20)
        cleaner = OverflowCleaner(store)
        handler = ToolResultOverflowHandler(store=store, cleaner=cleaner, max_chars=100, summary_chars=20)
        prefix = handler.make_prefix("/tmp/overflow/sess/call", 1, 3)
        assert prefix.startswith("[TOOL_RESULT_OVERFLOW]")
        assert "dir=/tmp/overflow/sess/call" in prefix
        assert "chunk=1/3" in prefix
        assert "*.full.txt=完整版" in prefix
        assert "*.summary.txt=摘要版" in prefix
        assert "≤20字符" in prefix


class TestStoreOverflow:
    @pytest.mark.asyncio
    async def test_store_overflow_chunks_and_returns_first(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        # Content larger than max_chars (100) to trigger chunking
        content = "x" * 250
        chunk_1, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
        )

        # Verify chunk 1 has prefix
        assert chunk_1.startswith("[TOOL_RESULT_OVERFLOW]")
        lines = chunk_1.split("\n", 1)
        assert "chunk=1/" in lines[0]

        # Verify ref
        assert ref.total_chars == 250
        # The store's max_chunk_size is 50, so 250 / 50 = 5 chunks
        assert ref.chunk_count == 5

    @pytest.mark.asyncio
    async def test_store_overflow_short_content(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        # Short content — single chunk
        content = "short content"
        chunk_1, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
        )

        assert chunk_1.startswith("[TOOL_RESULT_OVERFLOW]")
        assert ref.total_chars == 13
        assert ref.chunk_count == 1
