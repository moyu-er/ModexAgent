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
    async def test_store_overflow_returns_head_marker_tail_and_path_notice(
        self, handler: ToolResultOverflowHandler
    ) -> None:
        content = "H" * 100 + "M" * 250 + "T" * 100
        text, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
            max_chars=50,
        )

        # default ratios: head 10% / tail 15% of max_chars=50 → 5 / 7
        lines = text.split("\n")
        assert len(lines) == 4
        assert lines[0] == "H" * 5
        assert lines[2] == "T" * 7
        assert "OUTPUT ELIDED: 438 chars" in lines[1]
        assert lines[3].startswith(
            f"[Full output (450 chars total) saved to: {ref.dir_path}/full.txt"
        )
        assert not text.startswith("<")
        assert ref.total_chars == 450
        assert Path(ref.metadata_path) == Path(ref.dir_path, ".meta.json")
        assert {field.name for field in fields(ref)} == {
            "dir_path",
            "total_chars",
            "metadata_path",
        }

    @pytest.mark.asyncio
    async def test_store_overflow_short_content_unchanged(
        self, handler: ToolResultOverflowHandler
    ) -> None:
        content = "short content"
        text, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content=content,
            max_chars=50,
        )

        # nothing elided → no marker, no notice inflation
        assert text == content
        assert ref.total_chars == 13
        assert Path(ref.dir_path, "full.txt").read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_store_overflow_preserves_plain_text(
        self, handler: ToolResultOverflowHandler
    ) -> None:
        content = "hello [special] world"
        text, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_2",
            tool_name="read_file",
            content=content,
            max_chars=50,
        )

        assert text == content
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

        assert text == ""
        assert ref.total_chars == 0
        assert Path(ref.dir_path, "full.txt").read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_store_overflow_exactly_max_chars_unchanged(
        self, handler: ToolResultOverflowHandler
    ) -> None:
        content = "A" * 50
        text, ref = await handler.store_overflow(
            session_id="sess_1",
            tool_call_id="call_exact",
            tool_name="read_file",
            content=content,
            max_chars=50,
        )

        assert ref.total_chars == 50
        assert text == content

    @pytest.mark.asyncio
    async def test_store_overflow_custom_ratios(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()
        cleaner = OverflowCleaner(store)
        handler = ToolResultOverflowHandler(
            store=store, cleaner=cleaner, head_ratio=0.2, tail_ratio=0.3
        )
        try:
            content = "H" * 100 + "M" * 300 + "T" * 100
            text, ref = await handler.store_overflow(
                session_id="sess_1",
                tool_call_id="call_ratio",
                tool_name="read_file",
                content=content,
                max_chars=50,
            )
        finally:
            await cleaner.stop()

        # head_ratio 0.2 / tail_ratio 0.3 of max_chars=50 → 10 / 15
        lines = text.split("\n")
        assert lines[0] == "H" * 10
        assert lines[2] == "T" * 15
        assert "OUTPUT ELIDED: 475 chars" in lines[1]
        assert f"saved to: {ref.dir_path}/full.txt" in lines[3]
