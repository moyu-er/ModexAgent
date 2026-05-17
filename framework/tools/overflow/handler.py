from __future__ import annotations

from datetime import UTC, datetime

from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.overflow.models import OverflowMetadata, OverflowRef
from framework.tools.overflow.store import ToolOverflowStore


class ToolResultOverflowHandler:
    def __init__(self, store: ToolOverflowStore, cleaner: OverflowCleaner, max_chars: int = 10000, summary_chars: int = 200) -> None:
        self._store = store
        self._cleaner = cleaner
        self.max_chars = max_chars
        self.summary_chars = summary_chars

    @property
    def max_chunk_size(self) -> int:
        return max(int(self.max_chars * 0.9), self.max_chars - 200)

    def make_prefix(self, dir_path: str, chunk_index: int, total_chunks: int) -> str:
        return (
            f"[TOOL_RESULT_OVERFLOW] dir={dir_path}"
            f" | chunk={chunk_index}/{total_chunks}"
            f" | *.full.txt=完整版 *.summary.txt=摘要版(≤{self.summary_chars}字符)"
        )

    async def store_overflow(self, session_id: str, tool_call_id: str, tool_name: str, content: str) -> tuple[str, OverflowRef]:
        """Split content and store. Returns (chunk_1_full_content_with_prefix, OverflowRef)."""
        chunks = self._split(content)
        total_chars = len(content)
        total_chunks = len(chunks)

        _metadata = OverflowMetadata(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            session_id=session_id,
            created_at=datetime.now(UTC).isoformat(),
            total_chars=total_chars,
            total_chunks=total_chunks,
            max_chunk_size=self.max_chunk_size,
        )

        ref = await self._store.store(session_id, tool_call_id, tool_name, content)

        # Construct chunk 1 full content with prefix
        prefix = self.make_prefix(str(ref.dir_path), 1, total_chunks)
        chunk_1 = prefix + "\n" + chunks[0]
        return chunk_1, ref

    def schedule_cleanup(self, session_id: str, kept_call_ids: set[str]) -> None:
        self._cleaner.schedule_cleanup(session_id, kept_call_ids)

    def _split(self, text: str) -> list[str]:
        size = self.max_chunk_size
        return [text[i : i + size] for i in range(0, len(text), size)]
