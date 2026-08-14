from __future__ import annotations

from abc import ABC, abstractmethod

from modex_agent.tools.overflow.models import CleanRequest, OverflowMetadata, OverflowRef


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

        Also enforces *max_tool_call_ids* on the surviving set: the oldest
        kept entries are trimmed first.

        Args:
            request: CleanRequest describing what to keep.

        Returns:
            Number of entries deleted.
        """
        all_ids = await self.list_tool_call_ids(request.session_id)

        # 1. Delete entries whose call_id is no longer in session history.
        to_delete = [tcid for tcid in all_ids if tcid not in request.kept_call_ids]

        # 2. Enforce max count on the surviving (kept) entries.
        kept = [tcid for tcid in all_ids if tcid not in to_delete]
        if len(kept) > request.max_tool_call_ids:
            overflow = len(kept) - request.max_tool_call_ids
            to_delete.extend(kept[:overflow])

        count = 0
        for tcid in to_delete:
            if await self.delete(request.session_id, tcid):
                count += 1
        return count
