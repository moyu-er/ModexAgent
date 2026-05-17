from __future__ import annotations

from abc import ABC, abstractmethod

from framework.tools.overflow.models import CleanRequest, OverflowMetadata, OverflowRef


class ToolOverflowStore(ABC):
    """Abstract base class for tool result overflow storage.

    Implementations persist oversized tool results outside the main context,
    allowing the agent to reference them on-demand instead of embedding
    the full content in every turn.
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the store (create directories, validate config, etc.)."""
        pass

    @abstractmethod
    async def store(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> OverflowRef:
        """Store overflow content and return a reference to it.

        Args:
            session_id: The session identifier.
            tool_call_id: The unique tool call identifier.
            tool_name: The name of the tool that produced the content.
            content: The full overflow content to persist.

        Returns:
            OverflowRef pointing to the persisted data.
        """
        pass

    @abstractmethod
    async def read_chunk(
        self,
        session_id: str,
        tool_call_id: str,
        chunk_index: int,
        *,
        summary: bool = False,
    ) -> str | None:
        """Read a single chunk by index.

        Args:
            session_id: The session identifier.
            tool_call_id: The unique tool call identifier.
            chunk_index: 1-based chunk index.
            summary: If True, return the summary version; otherwise the full version.

        Returns:
            The chunk content including prefix, or None if not found.
        """
        pass

    @abstractmethod
    async def read_metadata(
        self,
        session_id: str,
        tool_call_id: str,
    ) -> OverflowMetadata | None:
        """Read metadata for a stored overflow entry.

        Args:
            session_id: The session identifier.
            tool_call_id: The unique tool call identifier.

        Returns:
            OverflowMetadata if found, otherwise None.
        """
        pass

    @abstractmethod
    async def delete(self, session_id: str, tool_call_id: str) -> bool:
        """Delete a stored overflow entry.

        Args:
            session_id: The session identifier.
            tool_call_id: The unique tool call identifier.

        Returns:
            True if the entry existed and was deleted.
        """
        pass

    @abstractmethod
    async def list_tool_call_ids(self, session_id: str) -> list[str]:
        """List all tool_call_ids for a session, sorted by created_at ascending.

        Args:
            session_id: The session identifier.

        Returns:
            Ordered list of tool_call_ids.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Release any resources held by the store."""
        pass

    async def clean(self, request: CleanRequest) -> int:
        """Remove overflow entries not in *kept_call_ids*.

        Args:
            request: CleanRequest describing what to keep.

        Returns:
            Number of entries deleted.
        """
        all_ids = await self.list_tool_call_ids(request.session_id)
        to_delete = [
            tcid
            for tcid in all_ids
            if tcid not in request.kept_call_ids
        ]
        # If there are too many entries, also delete oldest beyond max_tool_call_ids
        if len(all_ids) > request.max_tool_call_ids:
            overflow_count = len(all_ids) - request.max_tool_call_ids
            # Oldest are at the start (sorted by created_at ascending)
            for tcid in all_ids[:overflow_count]:
                if tcid not in to_delete:
                    to_delete.append(tcid)
        count = 0
        for tcid in to_delete:
            if await self.delete(request.session_id, tcid):
                count += 1
        return count
