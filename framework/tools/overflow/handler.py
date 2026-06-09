from __future__ import annotations

from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.overflow.models import OverflowRef
from framework.tools.overflow.store import ToolOverflowStore
from framework.utils.xml import xml_text


class ToolResultOverflowHandler:
    """Orchestrates overflow: store full content, return XML-wrapped first chunk.

    The returned message is a structured XML document containing chunk 1
    embedded in CDATA, plus metadata instructing the LLM how to read
    remaining chunks via the read tool.
    """

    # Template for the instruction element.  Kept as a class constant so the
    # LLM-facing text is centralised and can be overridden by subclasses.
    _INSTRUCTION_TEMPLATE = (
        "This result was too large and has been split into {chunk_count} chunk(s). "
        "Chunk 1 is already shown in the <chunk index=\"1\"> element below. "
        "To read rest chunks through {total_chunks}, use the read tool with "
        'path="{dir_path}/$CHUNK.full.txt", replacing $CHUNK with the number you need.'
    )

    def __init__(
        self,
        store: ToolOverflowStore,
        cleaner: OverflowCleaner,
    ) -> None:
        self._store = store
        self._cleaner = cleaner

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

        cdata = xml_text(chunk1)

        instruction = self._INSTRUCTION_TEMPLATE.format(
            chunk_count=ref.chunk_count,
            total_chunks=ref.chunk_count,
            dir_path=ref.dir_path,
        )

        xml = (
            f'<tool_result_overflow tool="{tool_name}" '
            f'total_chars="{ref.total_chars}" '
            f'total_chunks="{ref.chunk_count}" '
            f'current_chunk="1">\n'
            f'  <storage dir="{ref.dir_path}" session="{session_id}" tool_call="{tool_call_id}" />\n'
            f'  <instruction>\n'
            f'    {instruction}\n'
            f'  </instruction>\n'
            f'  <chunk index="1">{cdata}</chunk>\n'
            f'</tool_result_overflow>'
        )
        return xml, ref

    def schedule_cleanup(self, session_id: str, kept_call_ids: set[str]) -> None:
        self._cleaner.schedule_cleanup(session_id, kept_call_ids)
