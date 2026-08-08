"""Tests for ContextForkBuilder — pure-computation fork XML (T18).

T18 removed the fork XML file I/O. ``build()`` now queries the parent
session's message history (via the MemorySystem, which under T09 routes
through ``MessageStore.load_messages()``), applies lossy compaction, and
returns the XML string directly. No files are written; no registry; no
cleanup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.message import ChatMessage
from modex_agent.core.scope import MemoryContext
from modex_agent.ioc.configs.memory import (
    BudgetConfig,
    GovernanceConfig,
    MemoryConfig,
    SessionConfig,
)
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

    async def get_history(self, context: MemoryContext) -> list[ChatMessage]:
        self.calls.append(context)
        return list(self._messages)

    async def get_full_history(self, context: MemoryContext) -> list[ChatMessage]:
        self.calls.append(context)
        return list(self._messages)


def _msg(role: str, content: str, *, name: str | None = None) -> ChatMessage:
    return ChatMessage(role=role, content=content, name=name)


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
        template_memory=None,
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
async def test_build_returns_placeholder_when_no_memory_system(tmp_path: Path) -> None:
    """When subagent_memory_system is None, build() returns the placeholder
    XML. No files written."""
    builder = ContextForkBuilder()
    xml = await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=80,
        template_memory=None,
        subagent_memory_system=None,
        parent_name="main",
    )

    assert xml is not None
    assert "No parent messages available." in xml
    assert not (tmp_path / "fork_contexts").exists()


@pytest.mark.asyncio
async def test_build_returns_placeholder_when_history_empty(tmp_path: Path) -> None:
    """When the parent memory system returns no messages, build() returns the
    placeholder XML."""
    mem = _FakeMemorySystem([])

    builder = ContextForkBuilder()
    xml = await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=80,
        template_memory=None,
        subagent_memory_system=mem,  # type: ignore[arg-type]
        parent_name="main",
    )

    assert xml is not None
    assert "No parent messages available." in xml


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
        template_memory=None,
        subagent_memory_system=mem,  # type: ignore[arg-type]
        parent_name="main",
    )

    assert xml is not None
    assert "Inherited 3 messages" in xml
    # Last 3 (indices 7, 8, 9) survive; the rest are dropped.
    assert "msg-7" in xml and "msg-9" in xml
    assert "msg-0" not in xml and "msg-6" not in xml


@pytest.mark.asyncio
async def test_build_applies_budget_pruning(tmp_path: Path) -> None:
    """When template_memory.governance.budget is configured, build()
    applies ContextBudgetGovernance to the truncated messages.

    With a very small max_context_tokens and enough tool results, old
    tool results are replaced with _CLEARED_PLACEHOLDER.
    """
    from modex_agent.memory.context_governance import _CLEARED_PLACEHOLDER

    # Build 15 tool results, each 5000 chars → enough to trigger pruning
    # with max_context_tokens=50000, protect_tokens=40000, min_gain=20000.
    parent_messages: list[ChatMessage] = []
    for i in range(15):
        parent_messages.append(ChatMessage(role="user", content=f"u{i}"))
        parent_messages.append(ChatMessage(
            role="tool", name="read_file", tool_call_id=f"c{i}",
            content="x" * 5000,
        ))
    mem = _FakeMemorySystem(parent_messages)

    template_memory = MemoryConfig(
        session=SessionConfig(max_context_tokens=50_000),
        governance=GovernanceConfig(
            budget=BudgetConfig(
                governance_ratio=0.10,
                protect_tokens=4_000,
                min_gain_tokens=2_000,
                keep_recent=5,
            ),
        )
    )

    builder = ContextForkBuilder()
    xml = await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=80,
        template_memory=template_memory,
        subagent_memory_system=mem,  # type: ignore[arg-type]
        parent_name="main",
    )

    assert xml is not None
    # Some tool results should have been replaced with the placeholder.
    assert _CLEARED_PLACEHOLDER in xml
    assert not (tmp_path / "fork_contexts").exists()


@pytest.mark.asyncio
async def test_build_does_not_compact_when_budget_disabled(tmp_path: Path) -> None:
    """Without budget configured, oversized content survives verbatim
    (only the per-message 2000-char XML cap from _messages_to_xml applies)."""
    long_content = "y" * 1500  # under the 2000-char _messages_to_xml cap
    parent_messages = [_msg("user", long_content) for _ in range(5)]
    mem = _FakeMemorySystem(parent_messages)

    builder = ContextForkBuilder()
    xml = await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=80,
        template_memory=None,
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
        template_memory=None,
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
        async def get_history(self, context: MemoryContext) -> list[ChatMessage]:
            raise RuntimeError("memory exploded")

    mem = _BoomMemory()

    builder = ContextForkBuilder()
    xml = await builder.build(
        parent_session="conv.main",
        agent_type="scout",
        invocation_id="inv1",
        fork_max_messages=80,
        template_memory=None,
        subagent_memory_system=mem,  # type: ignore[arg-type]
        parent_name="main",
    )

    assert xml is not None
    assert "No parent messages available." in xml


def test_no_registry_attribute() -> None:
    """ContextForkBuilder no longer owns a _registry dict (T18 removed it)."""
    builder = ContextForkBuilder()
    assert not hasattr(builder, "_registry")
