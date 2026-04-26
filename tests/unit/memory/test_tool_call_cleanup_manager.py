from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from examples.bot_project.plugins.tool_call_cleanup.manager import ToolCallAwareSessionManager
from framework.memory.core.layers import SessionMemoryManager
from framework.memory.core.message import ChatMessage
from framework.memory.core.models import StorageRevision
from framework.memory.core.scope import MemoryContext


class DummySessionManager(SessionMemoryManager):
    def __init__(self) -> None:
        self.messages: list[ChatMessage] = []
        self.revision = StorageRevision(
            message_count=0,
            updated_at=datetime.now(UTC),
            version=7,
        )

    async def add_messages(
        self,
        context: MemoryContext,
        messages: list[ChatMessage | dict[str, Any]],
    ) -> StorageRevision:
        self.messages.extend(ChatMessage.coerce(message) for message in messages)
        self.revision = StorageRevision(
            message_count=len(self.messages),
            updated_at=datetime.now(UTC),
            version=self.revision.version + 1,
        )
        return self.revision

    async def get_visible_messages(
        self,
        context: MemoryContext,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        if limit is None:
            return list(self.messages)
        return list(self.messages[-limit:])

    async def get_all_messages(self, context: MemoryContext) -> list[ChatMessage]:
        return list(self.messages)

    async def save_checkpoint(
        self,
        context: MemoryContext,
        messages: list[ChatMessage | dict[str, Any]],
    ) -> None:
        pass

    async def load_checkpoint(self, context: MemoryContext) -> list[ChatMessage] | None:
        return None

    async def clear(self, context: MemoryContext) -> None:
        self.messages = []

    async def replace_messages(
        self,
        context: MemoryContext,
        messages: list[ChatMessage | dict[str, Any]],
    ) -> StorageRevision:
        self.messages = [ChatMessage.coerce(message) for message in messages]
        self.revision = StorageRevision(
            message_count=len(self.messages),
            updated_at=datetime.now(UTC),
            version=self.revision.version + 1,
        )
        return self.revision

    async def replace_messages_if_revision(
        self,
        context: MemoryContext,
        messages: list[ChatMessage | dict[str, Any]],
        expected_revision: StorageRevision,
        state_updates: Mapping[str, Any] | None = None,
    ) -> StorageRevision | None:
        _ = state_updates
        if expected_revision.version != self.revision.version:
            return None
        return await self.replace_messages(context, messages)

    async def get_revision(self, context: MemoryContext) -> StorageRevision:
        return self.revision


async def test_tool_call_cleanup_returns_inner_storage_revision() -> None:
    inner = DummySessionManager()
    manager = ToolCallAwareSessionManager(inner)

    revision = await manager.add_messages(
        MemoryContext(session_id="s1"),
        [{"role": "user", "content": "hello"}],
    )

    assert revision.updated_at is not None
    assert revision.version == 8
