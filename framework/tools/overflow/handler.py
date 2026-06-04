from __future__ import annotations

from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.overflow.models import OverflowRef
from framework.tools.overflow.store import ToolOverflowStore


def _wrap_cdata(text: str) -> str:
    """Wrap text in CDATA, handling embedded ]]> sequences."""
    if "]]>" not in text:
        return f"<![CDATA[{text}]]>"
    escaped = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{escaped}]]>"


class ToolResultOverflowHandler:
    """Orchestrates overflow: store full content, return XML-wrapped first chunk.

    The returned message is a structured XML document containing chunk 1
    embedded in CDATA, plus metadata instructing the LLM how to read
    remaining chunks via the read tool. The XML is marked with
    skip_overflow="true" for human readability; the interceptor's skip
    logic relies on ToolResult.overflow_processed, not this attribute.
    """

    def __init__(
        self,
        store: ToolOverflowStore,
        cleaner: OverflowCleaner,
        max_chars: int = 10_000,
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

        chunk1 = await self._store.read_chunk(session_id, tool_call_id, 1)
        if chunk1 is None:
            chunk1 = ""

        cdata = _wrap_cdata(chunk1)

        xml = (
            f'<tool_result_overflow tool="{tool_name}" '
            f'total_chars="{ref.total_chars}" '
            f'total_chunks="{ref.chunk_count}" '
            f'current_chunk="1" '
            f'max_chunk_size="{ref.max_chunk_size}" '
            f'skip_overflow="true">\n'
            f'  <storage dir="{ref.dir_path}" session="{session_id}" tool_call="{tool_call_id}" />\n'
            f'  <instruction>\n'
            f'    This result was too large and has been split into {ref.chunk_count} chunk(s) '
            f'of ~{ref.max_chunk_size} chars each. Use the read tool with '
            f'path="{ref.dir_path}/N.full.txt" to load any chunk. This message itself '
            f'is already processed — no further overflow handling is needed.\n'
            f'  </instruction>\n'
            f'  <chunk index="1">{cdata}</chunk>\n'
            f'</tool_result_overflow>'
        )
        return xml, ref

    def schedule_cleanup(self, session_id: str, kept_call_ids: set[str]) -> None:
        self._cleaner.schedule_cleanup(session_id, kept_call_ids)
