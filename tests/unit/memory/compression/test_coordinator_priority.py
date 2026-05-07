from __future__ import annotations

from typing import Any

from framework.core.types import MessageRole
from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator
from framework.memory.context_governance import META_CONTEXT_LOSSY
from framework.memory.core.message import ChatMessage
from framework.memory.core.models import StorageRevision
from framework.memory.core.scope import MemoryContext


class _Session:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = [ChatMessage.from_dict(m) for m in messages]
        self.revision = StorageRevision(version=1, message_count=len(messages), updated_at=None)  # type: ignore[arg-type]

    async def get_all_messages(self, context: MemoryContext) -> list[ChatMessage]:
        return self.messages

    async def get_revision(self, context: MemoryContext) -> StorageRevision:
        return self.revision

    async def replace_messages_if_revision(
        self,
        context: MemoryContext,
        messages: list[dict[str, Any]],
        expected: StorageRevision,
        extra_state: dict[str, Any] | None = None,
    ) -> StorageRevision | None:
        assert expected == self.revision
        self.messages = [ChatMessage.from_dict(m) for m in messages]
        self.revision = StorageRevision(version=2, message_count=len(messages), updated_at=None)  # type: ignore[arg-type]
        return self.revision


class _Archive:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def append(self, context: MemoryContext, entry: Any) -> None:
        self.entries.append(entry)


async def test_compression_keeps_latest_user_within_hard_message_ratio() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "old"},
        {"role": MessageRole.ASSISTANT, "content": "old answer"},
        {"role": MessageRole.AGENT, "source_agent": "peer", "content": "[From Agent peer]\nagent"},
        {"role": MessageRole.USER, "content": "latest human"},
        {"role": MessageRole.ASSISTANT, "content": "working"},
    ]
    session = _Session(messages)
    archive = _Archive()
    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=4,
        max_tokens=None,
        keep_ratio_for_messages=0.5,
    )

    result = await coordinator.maybe_compress(
        session=session,
        archive=archive,
        context=MemoryContext(session_id="s1", user_id="u1"),
    )

    kept = [m.to_dict() for m in session.messages]
    assert result.committed is True
    assert len(kept) <= 2
    assert kept[0]["content"] == "latest human"
    assert kept[1]["content"] == "working"


async def test_compression_user_beats_agent_when_budget_allows_one() -> None:
    messages = [
        {"role": MessageRole.AGENT, "source_agent": "peer", "content": "[From Agent peer]\nagent"},
        {"role": MessageRole.USER, "content": "human"},
    ]
    session = _Session(messages)
    archive = _Archive()
    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=1,
        max_tokens=None,
        keep_ratio_for_messages=1.0,
    )

    await coordinator.maybe_compress(
        session=session,
        archive=archive,
        context=MemoryContext(session_id="s1", user_id="u1"),
    )

    kept = [m.to_dict() for m in session.messages]
    assert kept == [{"role": "user", "content": "human"}]


async def test_compression_does_not_persist_lossy_content() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "old"},
        {"role": MessageRole.TOOL, "tool_call_id": "x", "content": "x" * 4000},
        {"role": MessageRole.USER, "content": "new"},
    ]
    session = _Session(messages)
    archive = _Archive()
    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=2,
        max_tokens=None,
        keep_ratio_for_messages=0.5,
    )

    await coordinator.maybe_compress(
        session=session,
        archive=archive,
        context=MemoryContext(session_id="s1", user_id="u1"),
    )

    kept = [m.to_dict() for m in session.messages]
    assert all(META_CONTEXT_LOSSY not in msg for msg in kept)
    assert all("omitted from context" not in str(msg.get("content", "")) for msg in kept)
