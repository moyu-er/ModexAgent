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
    async def test_store_overflow_returns_xml(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        content = "x" * 250
        xml, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
        )

        assert xml.startswith('<tool_result_overflow')
        assert 'tool="read_file"' in xml
        assert 'total_chars="250"' in xml
        assert 'total_chunks="5"' in xml
        assert 'current_chunk="1"' in xml
        assert 'skip_overflow="true"' in xml
        assert '<storage dir=' in xml
        assert '<instruction>' in xml
        assert '<chunk index="1"><![CDATA[' in xml
        assert ref.total_chars == 250
        assert ref.chunk_count == 5

    @pytest.mark.asyncio
    async def test_store_overflow_short_content(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        content = "short content"
        xml, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
        )

        assert xml.startswith('<tool_result_overflow')
        assert ref.total_chars == 13
        assert ref.chunk_count == 1
        assert "short content" in xml

    @pytest.mark.asyncio
    async def test_store_overflow_escapes_cdata(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        content = "hello ]]> world"
        xml, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_2",
            tool_name="read_file",
            content=content,
        )

        assert "hello ]]]]><![CDATA[> world" in xml
