from __future__ import annotations

from pathlib import Path

import pytest

from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.overflow.handler import ToolResultOverflowHandler
from framework.tools.overflow.local import LocalFileToolOverflowStore


@pytest.fixture
async def handler(tmp_path: Path) -> ToolResultOverflowHandler:
    store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50)
    await store.initialize()
    cleaner = OverflowCleaner(store)
    h = ToolResultOverflowHandler(store=store, cleaner=cleaner)
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
        assert '<storage dir=' in xml
        assert '<instruction>' in xml
        assert '<chunk index="1">' in xml
        # Chunk content is present (xml_text skips CDATA when no special chars)
        assert "x" * 50 in xml
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

    @pytest.mark.asyncio
    async def test_store_overflow_empty_content(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        """Empty content produces 1 chunk with empty CDATA."""
        xml, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_empty",
            tool_name="read_file",
            content="",
        )

        assert xml.startswith('<tool_result_overflow')
        assert ref.total_chars == 0
        assert ref.chunk_count == 1
        # Empty content: chunk element exists with empty text (no CDATA needed)
        assert '<chunk index="1"></chunk>' in xml

    @pytest.mark.asyncio
    async def test_store_overflow_exactly_max_chunk_size(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        """Content of exactly max_chunk_size chars produces exactly 1 chunk."""
        content = "A" * 50  # fixture uses max_chunk_size=50
        xml, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_exact",
            tool_name="read_file",
            content=content,
        )

        assert ref.total_chars == 50
        assert ref.chunk_count == 1
        assert "A" * 50 in xml

    @pytest.mark.asyncio
    async def test_store_overflow_instruction_template_fields(self, tmp_path: Path, handler: ToolResultOverflowHandler) -> None:
        """Instruction text interpolates all template variables from ref."""
        content = "x" * 250  # 250 / 50 = 5 chunks
        xml, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_tmpl",
            tool_name="read_file",
            content=content,
        )

        assert "split into 5 chunk(s)" in xml
        assert '$CHUNK.full.txt' in xml
        assert str(ref.dir_path) in xml
