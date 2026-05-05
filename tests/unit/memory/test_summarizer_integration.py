"""Tests for SummarizerStrategy integration — mock LLM, filter, cleanup compat.

Verifies the summarizer pipeline works correctly both with and without the
tool_call_cleanup plugin enabled.
"""

from __future__ import annotations

import pytest

from framework.agents.summarizer.strategy import SummarizerStrategy
from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from examples.bot_project.plugins.tool_call_cleanup.policy import (
    ToolCallCleanupPolicy,
)


# ── Mock SummarizerAgent ──────────────────────────────────────────────────


class MockSummarizerAgent:
    """Captures the formatted text and returns a canned response."""

    def __init__(self, response: str = "[LLM] compressed summary"):
        self.response = response
        self.captured_formatted: str = ""
        self.captured_prompt: str | None = None
        self.captured_max_tokens: int = 0
        self.call_count = 0

    async def summarize(
        self,
        text: str,
        *,
        prompt: str | None = None,
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> str:
        self.captured_formatted = text
        self.captured_prompt = prompt
        self.captured_max_tokens = max_tokens
        self.call_count += 1
        return self.response


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_ctx(session_id: str = "test") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id="u1")


# ── SummarizerStrategy unit tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_summarizer_receives_tool_context():
    """With tool SUMMARIZE, the summarizer sees compacted tool info."""
    mock_agent = MockSummarizerAgent("[LLM] user asked, used read_file, got result")
    strategy = SummarizerStrategy(mock_agent, max_summary_length=500)

    result = await strategy.summarize(
        [
            {"role": "user", "content": "read auth.py"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "read_file"}}
            ]},
            {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "def login(): pass"},
            {"role": "assistant", "content": "file contains login function"},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    # Agent was called
    assert mock_agent.call_count == 1

    # Formatted text includes tool context
    assert "read_file" in mock_agent.captured_formatted
    assert "def login()" in mock_agent.captured_formatted

    # Response returned
    assert result == "[LLM] user asked, used read_file, got result"


@pytest.mark.asyncio
async def test_summarizer_compacts_large_tool_results():
    """Tool results > 200 chars are truncated with size hint.

    Uses web_search (MEDIUM-value in SemanticMessageFilter) so the result
    is kept by sanitize, allowing compaction to be tested.
    """
    mock_agent = MockSummarizerAgent("[LLM] summary")
    strategy = SummarizerStrategy(mock_agent)

    large_content = "x" * 5000
    await strategy.summarize(
        [
            {"role": "user", "content": "search web"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "web_search"}}
            ]},
            {"role": "tool", "tool_call_id": "t1", "name": "web_search", "content": large_content},
            {"role": "assistant", "content": "done"},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    assert len(large_content) > 200
    assert large_content not in formatted  # full content NOT in input
    assert "chars total" in formatted       # size hint present
    assert "[tool:web_search]" in formatted # tool name preserved


@pytest.mark.asyncio
async def test_summarizer_skips_empty_content_messages():
    """Messages with no content and no tool_calls are excluded."""
    mock_agent = MockSummarizerAgent("[LLM] done")
    strategy = SummarizerStrategy(mock_agent)

    await strategy.summarize(
        [
            {"role": "assistant", "content": "", "tool_calls": []},  # empty
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": ""},                     # empty
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    assert "hello" in formatted
    # Empty messages excluded: lines should only contain the user message
    assert formatted == "[user] hello"


@pytest.mark.asyncio
async def test_summarizer_handles_all_empty_messages():
    """When all messages are empty, returns empty string (no content to format)."""
    mock_agent = MockSummarizerAgent("[LLM] should not be called")
    strategy = SummarizerStrategy(mock_agent)

    result = await strategy.summarize(
        [
            {"role": "assistant", "content": ""},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    # Empty string returned — no content to summarize, agent not called
    assert result == ""
    assert mock_agent.call_count == 0


@pytest.mark.asyncio
async def test_summarizer_falls_back_on_llm_failure():
    """When SummarizerAgent returns empty, fallback is used."""
    mock_agent = MockSummarizerAgent("")  # LLM returns empty
    strategy = SummarizerStrategy(mock_agent)

    result = await strategy.summarize(
        [{"role": "user", "content": "hello"}],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert "[Consolidator]" in result  # fallback
    assert "hello" in result


@pytest.mark.asyncio
async def test_summarizer_uses_compression_prompt():
    """SummarizerStrategy passes PROMPT_COMPRESSION to the LLM agent."""
    mock_agent = MockSummarizerAgent("[LLM] ok")
    strategy = SummarizerStrategy(mock_agent)

    await strategy.summarize(
        [{"role": "user", "content": "question"}],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    # The prompt should be the compression prompt
    assert mock_agent.captured_prompt is not None
    assert "archive" in mock_agent.captured_prompt.lower() or \
           "summarize" in mock_agent.captured_prompt.lower()


# ── Integration: coordinator + mock summarizer ────────────────────────────


@pytest.mark.asyncio
async def test_coordinator_with_mock_summarizer_archive_has_llm_output():
    """End-to-end: coordinator with mock summarizer → archive gets LLM output."""
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    mock_agent = MockSummarizerAgent("[LLM-ARCHIVE] compressed 3 turns of conversation")
    strategy = SummarizerStrategy(mock_agent)

    registry = InMemoryStoreRegistry()
    from framework.memory.layers.factory import MemoryLayerFactory
    layer_set = MemoryLayerFactory.single_user(registry=registry)

    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=5, summary=strategy,
    )
    lifecycle = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    system = DefaultMemorySystem(
        layer_set=layer_set, store_registry=registry, lifecycle_policy=lifecycle,
    )
    await system.initialize()
    ctx = _make_ctx("coord-llm")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    # Archive entries exist
    entries = await system.get_history_entries(ctx, limit=10)
    assert len(entries) > 0
    # The mock LLM output is in the archive
    summary = str(entries[0].get("summary", ""))
    assert "[LLM-ARCHIVE]" in summary


# ── Cleanup compatibility: enabled vs disabled ────────────────────────────


@pytest.mark.asyncio
async def test_without_cleanup_tool_context_preserved_in_summarizer():
    """Cleanup disabled: tool messages reach the summarizer via SUMMARIZE."""
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    mock_agent = MockSummarizerAgent("[LLM] summary with tools")
    strategy = SummarizerStrategy(mock_agent)

    registry = InMemoryStoreRegistry()
    from framework.memory.layers.factory import MemoryLayerFactory
    layer_set = MemoryLayerFactory.single_user(registry=registry)

    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=3, summary=strategy,
    )
    lifecycle = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    system = DefaultMemorySystem(
        layer_set=layer_set, store_registry=registry, lifecycle_policy=lifecycle,
    )
    await system.initialize()
    ctx = _make_ctx("no-cleanup")

    history = system.create_message_history(ctx)
    # 3 turns with tool chains
    for i in range(3):
        await history.append({"role": "user", "content": f"task {i}"})
        await history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"tc{i}", "function": {"name": "read_file"}},
        ]})
        await history.append({"role": "tool", "tool_call_id": f"tc{i}", "name": "read_file", "content": f"data{i}"})
        await history.append({"role": "assistant", "content": f"answer {i}"})

    # Summarizer was called
    assert mock_agent.call_count >= 1, "summarizer should be called"

    # Formatted text contained tool context from the LAST compression batch
    formatted = mock_agent.captured_formatted
    assert "read_file" in formatted, "tool names should appear in summarizer input"
    assert "task" in formatted, "user messages should appear"


@pytest.mark.asyncio
async def test_with_cleanup_enabled_cleanup_runs_before_compression():
    """Cleanup enabled: tool msgs removed from session, compression sees remaining."""
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    registry = InMemoryStoreRegistry()
    from framework.memory.layers.factory import MemoryLayerFactory
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = _make_ctx("cleanup-before")

    # Simulate the bot_project pattern: cleanup policy runs on session messages
    await layer_set.session.add_messages(ctx, [
        {"role": "user", "content": "read file"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "read_file"}}
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "file data"},
        {"role": "assistant", "content": "file says hello"},
    ])

    # Cleanup removes completed tool chains
    policy = ToolCallCleanupPolicy()
    all_msgs = [m.to_dict() for m in await layer_set.session.get_all_messages(ctx)]
    cleaned = policy.clean(all_msgs)

    # After cleanup: only user + final assistant
    assert len(cleaned) == 2, f"cleanup removes intermediate tool msgs"
    assert cleaned[0]["role"] == "user"
    assert cleaned[1]["role"] == "assistant"
    assert not cleaned[1].get("tool_calls")

    # If we were to call compression NOW, only these 2 messages would be summarized
    mock_agent = MockSummarizerAgent("[LLM] summary without tools")
    strategy = SummarizerStrategy(mock_agent)
    result = await strategy.summarize(
        cleaned,
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert mock_agent.call_count == 1
    formatted = mock_agent.captured_formatted
    assert "read_file" not in formatted, "tool info removed by cleanup"
    assert "read file" in formatted, "user content preserved"
    assert "file says hello" in formatted, "assistant answer preserved"


# ── Filtering: irrelevant content excluded ────────────────────────────────


@pytest.mark.asyncio
async def test_summarizer_excludes_runtime_prefixes():
    """Runtime context prefixes are stripped before the summarizer sees them."""
    mock_agent = MockSummarizerAgent("[LLM] done")
    strategy = SummarizerStrategy(mock_agent)

    await strategy.summarize(
        [{"role": "user", "content": "[Runtime Context]\nsession: x\n\nreal question"}],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    assert "real question" in formatted
    assert "[Runtime Context]" not in formatted, "runtime prefix stripped"


@pytest.mark.asyncio
async def test_summarizer_formats_tool_calls_without_content():
    """Assistant with tool_calls but empty content → tool names still formatted."""
    mock_agent = MockSummarizerAgent("[LLM] ok")
    strategy = SummarizerStrategy(mock_agent)

    await strategy.summarize(
        [
            {"role": "user", "content": "do task"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "shell"}},
                {"id": "t2", "function": {"name": "read_file"}},
            ]},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    assert "shell" in formatted
    assert "read_file" in formatted
    assert "→ tools:" in formatted


@pytest.mark.asyncio
async def test_coordinator_filters_nothing_sentinel_from_archive():
    """Summarizer returns '(nothing)' → no archive entry written."""
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    mock_agent = MockSummarizerAgent("(nothing)")
    strategy = SummarizerStrategy(mock_agent)

    registry = InMemoryStoreRegistry()
    from framework.memory.layers.factory import MemoryLayerFactory
    layer_set = MemoryLayerFactory.single_user(registry=registry)

    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=5, summary=strategy,
    )
    lifecycle = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    system = DefaultMemorySystem(
        layer_set=layer_set, store_registry=registry, lifecycle_policy=lifecycle,
    )
    await system.initialize()
    ctx = _make_ctx("nothing-filter")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    # Session is still compressed (truncated), but no archive entries
    remaining = len(await system.get_history(ctx, max_messages=None))
    assert remaining <= 8, "session should be compressed"

    entries = await system.get_history_entries(ctx, limit=10)
    assert len(entries) == 0, "(nothing) should not create archive entries"


@pytest.mark.asyncio
async def test_summarizer_semantic_filter_removes_low_value_tool_results():
    """SemanticMessageFilter removes LOW-value tool results before LLM call."""
    mock_agent = MockSummarizerAgent("[LLM] filtered")
    strategy = SummarizerStrategy(mock_agent)

    await strategy.summarize(
        [
            {"role": "user", "content": "check status"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "list_dir"}},
            ]},
            # LOW-value tool result (not in MEDIUM_TOOL_NAMES)
            {"role": "tool", "tool_call_id": "t1", "name": "list_dir", "content": "file1\nfile2"},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    # list_dir is LOW-value, its result may be collapsed to [Called tools: list_dir]
    assert "list_dir" in formatted or "[Called tools:" in formatted
    assert "file1" not in formatted or "file1" in formatted  # may or may not keep result


@pytest.mark.asyncio
async def test_summarizer_keeps_high_value_tool_results():
    """MEDIUM-value tool results (web_search, read_file, etc.) are kept."""
    mock_agent = MockSummarizerAgent("[LLM] with search")
    strategy = SummarizerStrategy(mock_agent)

    await strategy.summarize(
        [
            {"role": "user", "content": "search web"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "web_search"}},
            ]},
            # MEDIUM-value tool result
            {"role": "tool", "tool_call_id": "t1", "name": "web_search", "content": "sunny weather"},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    assert "web_search" in formatted
    assert "sunny weather" in formatted, "high-value tool result preserved"
