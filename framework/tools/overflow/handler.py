from __future__ import annotations

from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.overflow.models import OverflowRef
from framework.tools.overflow.store import ToolOverflowStore

_PREVIEW_CHARS = 500


class ToolResultOverflowHandler:
    """Orchestrates overflow: store full content, return short notice.

    The returned message is a brief truncation notice + preview —
    deliberately short so it never triggers another overflow cycle.
    Full chunks are on disk for retrieval via read_chunk.
    """

    def __init__(
        self,
        store: ToolOverflowStore,
        cleaner: OverflowCleaner,
        max_chars: int = 10000,
    ) -> None:
        self._store = store
        self._cleaner = cleaner
        self.max_chars = max_chars

    async def store_overflow(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> tuple[str, OverflowRef]:
        ref = await self._store.store(session_id, tool_call_id, tool_name, content)

        preview = content[:_PREVIEW_CHARS]
        if len(content) > _PREVIEW_CHARS:
            preview += "..."

        notice = (
            f"[TOOL_RESULT_TRUNCATED]\n"
            f"The {tool_name} result was too large ({len(content)} chars) "
            f"and has been truncated.\n"
            f"Full content saved in {ref.chunk_count} file(s) at:\n"
            f"  {ref.dir_path}\n"
            f"Files are named 1.full.txt, 2.full.txt, ... "
            f"{ref.chunk_count}.full.txt\n"
            f"Read the content you need by chunk file.\n"
            f"\n"
            f"Preview (first {_PREVIEW_CHARS} chars):\n"
            f"{preview}"
        )
        return notice, ref

    def schedule_cleanup(self, session_id: str, kept_call_ids: set[str]) -> None:
        self._cleaner.schedule_cleanup(session_id, kept_call_ids)
