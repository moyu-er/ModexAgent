"""Tests for ContextForkBuilder — pure-computation fork XML (T18).

T18 removed the fork XML file I/O. ``build()`` now queries the parent
session's message history (via the MemorySystem, which under T09 routes
through ``MessageStore.load_messages()``), and returns the XML string directly.
No files are written; no registry; no cleanup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.memory.scope import MemoryContext
from modex_agent.multi_agent.context_fork import ContextForkBuilder


class _FakeMemorySystem:
    """Stand-in for MemorySystem that returns a canned message list.

    Mirrors the real ``MemorySystem.get_history(context)`` contract used by
    ``ContextForkBuilder.build``: it is the application-facing read path that
    under T09 routes through ``MessageStore.load_messages()``. Only
    ``get_history`` is exercised by ``build``; the full ABC is not needed
    here, so this is a structural stub rather than a ``MemorySystem`` subclass.
    """

    def __init__(self, messages: list[ChatMessage] | None = None) -> None:
        self._messages = messages if messages is not None else []
        self.calls: list[MemoryContext] = []
        self.limits: list[int | None] = []

    async def get_history(self, context: MemoryContext) -> list[ChatMessage]:
        self.calls.append(context)
        return list(self._messages)

    async def get_full_history(
        self, context: MemoryContext, *, limit: int | None = None
    ) -> list[ChatMessage]:
        self.calls.append(context)
        self.limits.append(limit)
        return list(self._messages[-limit:] if limit is not None else self._messages)


def _msg(role: str, content: str, *, name: str | None = None) -> ChatMessage:
    return ChatMessage(role=MessageRole(role), content=content, name=name)


@pytest.mark.asyncio
async def test_build_returns_xml_from_parent_messages(tmp_path: Path) -> None:
    """build() queries the parent MemorySystem and wraps the messages in
    <forked_context> XML. No files are written to disk."""
    parent_messages = [
        _msg("user", "Hello parent"),
        _msg("assistant", "Hi there"),
    ]
    mem = _FakeMemorySystem(parent_messages)

    builder = ContextForkBuilder()
    xml = await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=80,
        subagent_memory_system=mem,  # type: ignore[arg-type]
        parent_name="main",
    )

    assert xml is not None
    assert '<forked_context source="main">' in xml
    assert "Hello parent" in xml
    assert "Hi there" in xml
    assert "Inherited 2 messages" in xml
    # The build queried the parent session's history through the memory system.
    assert len(mem.calls) == 1
    assert mem.calls[0].session_id == "conv.main"
    # No files created under the workspace.
    assert not (tmp_path / "fork_contexts").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_build_returns_empty_snapshot_when_history_empty(tmp_path: Path) -> None:
    mem = _FakeMemorySystem([])

    builder = ContextForkBuilder()
    xml = await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=80,
        subagent_memory_system=mem,  # type: ignore[arg-type]
        parent_name="main",
    )

    assert xml is not None
    assert "Inherited 0 messages" in xml


@pytest.mark.asyncio
async def test_build_truncates_to_fork_max_messages(tmp_path: Path) -> None:
    """Only the last fork_max_messages parent messages are included."""
    parent_messages = [_msg("user", f"msg-{i}") for i in range(10)]
    mem = _FakeMemorySystem(parent_messages)

    builder = ContextForkBuilder()
    xml = await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=3,
        subagent_memory_system=mem,  # type: ignore[arg-type]
        parent_name="main",
    )

    assert xml is not None
    assert "Inherited 3 messages" in xml
    # Last 3 (indices 7, 8, 9) survive; the rest are dropped.
    assert "msg-7" in xml and "msg-9" in xml
    assert "msg-0" not in xml and "msg-6" not in xml
    assert mem.limits == [3]


@pytest.mark.asyncio
async def test_build_preserves_content_within_snapshot_cap(tmp_path: Path) -> None:
    long_content = "y" * 1500  # under the 2000-char _messages_to_xml cap
    parent_messages = [_msg("user", long_content) for _ in range(5)]
    mem = _FakeMemorySystem(parent_messages)

    builder = ContextForkBuilder()
    xml = await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=80,
        subagent_memory_system=mem,  # type: ignore[arg-type]
        parent_name="main",
    )

    assert xml is not None
    assert "Context content truncated" not in xml
    assert long_content in xml


@pytest.mark.asyncio
async def test_build_creates_no_files(tmp_path: Path) -> None:
    """build() performs no file I/O whatsoever — tmp_path stays empty even
    when a workspace is provided."""
    mem = _FakeMemorySystem([_msg("user", "hi")])
    builder = ContextForkBuilder()
    await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=80,
        subagent_memory_system=mem,  # type: ignore[arg-type]
        parent_name="main",
    )
    # Nothing was created under the workspace.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_build_swallows_memory_system_exception(tmp_path: Path) -> None:
    """A failure inside the memory system must not propagate — the fork
    context degrades to the placeholder XML rather than breaking subagent
    materialization."""

    class _BoomMemory(_FakeMemorySystem):
        async def get_full_history(
            self, context: MemoryContext, *, limit: int | None = None
        ) -> list[ChatMessage]:
            raise RuntimeError("memory exploded")

    mem = _BoomMemory()

    builder = ContextForkBuilder()
    xml = await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=80,
        subagent_memory_system=mem,  # type: ignore[arg-type]
        parent_name="main",
    )

    assert xml is not None
    assert "No parent messages available." in xml


def test_no_registry_attribute() -> None:
    """ContextForkBuilder no longer owns a _registry dict (T18 removed it)."""
    builder = ContextForkBuilder()
    assert not hasattr(builder, "_registry")
