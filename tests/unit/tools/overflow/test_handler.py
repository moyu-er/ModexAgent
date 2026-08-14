from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import fields
from pathlib import Path

import pytest

from modex_agent.tools.overflow.cleaner import OverflowCleaner
from modex_agent.tools.overflow.handler import ToolResultOverflowHandler
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore


@pytest.fixture
async def handler(tmp_path: Path) -> AsyncGenerator[ToolResultOverflowHandler]:
    store = LocalFileToolOverflowStore(workspace=tmp_path)
    await store.initialize()
    cleaner = OverflowCleaner(store)
    h = ToolResultOverflowHandler(store=store, cleaner=cleaner)
    yield h
    await cleaner.stop()


class TestStoreOverflow:
    @pytest.mark.asyncio
    async def test_store_overflow_returns_prefix_and_file_notice(
        self, handler: ToolResultOverflowHandler
    ) -> None:
        content = "x" * 250
        text, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
            max_chars=50,
        )

        assert text.startswith("x" * 50)
        assert f"[Full output (250 chars total) saved to: {ref.dir_path}/full.txt]" in text
        assert not text.startswith("<")
        assert ref.total_chars == 250
        assert Path(ref.metadata_path) == Path(ref.dir_path, ".meta.json")
        assert {field.name for field in fields(ref)} == {
            "dir_path",
            "total_chars",
            "metadata_path",
        }

    @pytest.mark.asyncio
    async def test_store_overflow_short_content(self, handler: ToolResultOverflowHandler) -> None:
        content = "short content"
        text, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
            max_chars=50,
        )

        assert text.startswith("short content\n\n[Full output")
        assert ref.total_chars == 13
        assert Path(ref.dir_path, "full.txt").read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_store_overflow_preserves_plain_text(self, handler: ToolResultOverflowHandler) -> None:
        content = "hello [special] world"
        text, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_2",
            tool_name="read_file",
            content=content,
            max_chars=50,
        )

        assert text.startswith(content)
        assert Path(ref.dir_path, "full.txt").read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_store_overflow_empty_content(self, handler: ToolResultOverflowHandler) -> None:
        text, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_empty",
            tool_name="read_file",
            content="",
            max_chars=50,
        )

        assert text.startswith("\n\n[Full output (0 chars total) saved to:")
        assert ref.total_chars == 0
        assert Path(ref.dir_path, "full.txt").read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_store_overflow_exactly_max_chars(self, handler: ToolResultOverflowHandler) -> None:
        content = "A" * 50
        text, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_exact",
            tool_name="read_file",
            content=content,
            max_chars=50,
        )

        assert ref.total_chars == 50
        assert text.startswith(content + "\n\n[Full output")

    @pytest.mark.asyncio
    async def test_store_overflow_notice_points_to_complete_file(
        self, handler: ToolResultOverflowHandler
    ) -> None:
        content = "x" * 250
        text, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_tmpl",
            tool_name="read_file",
            content=content,
            max_chars=50,
        )

        assert text == (
            "x" * 50
            + f"\n\n[Full output (250 chars total) saved to: {ref.dir_path}/full.txt]"
        )
