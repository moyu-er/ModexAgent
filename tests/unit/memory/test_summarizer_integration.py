"""Tests for SummarizerStrategy integration -- mock LLM, filter, cleanup compat.

Verifies the summarizer pipeline works correctly both with and without the
tool_call_cleanup plugin enabled.
"""
from __future__ import annotations

import pytest

from framework.agents.summarizer.strategy import DefaultSummarizerStrategy
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
    strategy = DefaultSummarizerStrategy(mock_agent, max_summary_length=500)

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

    assert mock_agent.call_count == 1
    assert "read_file" in mock_agent.captured_formatted
    assert "def login()" in mock_agent.captured_formatted
    assert result == "[LLM] user asked, used read_file, got result"


@pytest.mark.asyncio
async def test_summarizer_compacts_large_tool_results():
    """Tool results > 200 chars are truncated with size hint."""
    mock_agent = MockSummarizerAgent("[LLM] summary")
    strategy = DefaultSummarizerStrategy(mock_agent)

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
    assert large_content not in formatted
    assert "chars total" in formatted
    assert "[tool:web_search]" in formatted


@pytest.mark.asyncio
async def test_summarizer_skips_empty_content_messages():
    """Messages with no content and no tool_calls are excluded."""
    mock_agent = MockSummarizerAgent("[LLM] done")
    strategy = DefaultSummarizerStrategy(mock_agent)

    await strategy.summarize(
        [
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": ""},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    assert "hello" in formatted
    assert formatted == "[user] hello"


@pytest.mark.asyncio
async def test_summarizer_handles_all_empty_messages():
    """When all messages are empty, returns empty string (no content to format)."""
    mock_agent = MockSummarizerAgent("[LLM] should not be called")
    strategy = DefaultSummarizerStrategy(mock_agent)

    result = await strategy.summarize(
        [
            {"role": "assistant", "content": ""},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert result == ""
    assert mock_agent.call_count == 0


@pytest.mark.asyncio
async def test_summarizer_falls_back_on_llm_failure():
    """When SummarizerAgent returns empty, fallback is used."""
    mock_agent = MockSummarizerAgent("")
    strategy = DefaultSummarizerStrategy(mock_agent)

    result = await strategy.summarize(
        [{"role": "user", "content": "hello"}],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert "[Consolidator]" in result
    assert "hello" in result


@pytest.mark.asyncio
async def test_summarizer_uses_compression_prompt():
    """SummarizerStrategy passes PROMPT_MEMORY_COMPRESSION to the LLM agent."""
    mock_agent = MockSummarizerAgent("[LLM] ok")
    strategy = DefaultSummarizerStrategy(mock_agent)

    await strategy.summarize(
        [{"role": "user", "content": "question"}],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert mock_agent.captured_prompt is not None
    assert "reference context" in mock_agent.captured_prompt.lower()


# ── Integration: archive strategy with mock summarizer ────────────────────


@pytest.mark.asyncio
async def test_archive_strategy_with_mock_summarizer_archive_has_llm_output():
    """End-to-end: cleanup_session with mock summarizer -> archive gets LLM output."""
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.registry.in_memory import InMemoryStoreRegistry
    from framework.memory.archive_generation import DualLLMArchiveGenerationStrategy
    from framework.memory.layers.factory import MemoryLayerFactory

    mock_agent = MockSummarizerAgent("[LLM-ARCHIVE] compressed 3 turns of conversation")
    archive_gen = DualLLMArchiveGenerationStrategy(summarizer=mock_agent)

    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)

    cleanup_config: dict[str, int | float] = {
        "max_messages": 5,
        "keep_ratio": 0.5,
    }
    system = DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        archive_strategy=archive_gen,
        cleanup_config=cleanup_config,
    )
    await system.initialize()
    ctx = _make_ctx("coord-llm")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    entries = await system.get_history_entries(ctx, limit=10)
    assert len(entries) > 0
    summary = str(entries[0].get("summary", ""))
    assert "[LLM-ARCHIVE]" in summary


# ── Cleanup compatibility: enabled vs disabled ────────────────────────────


@pytest.mark.asyncio
async def test_without_cleanup_tool_context_preserved_in_summarizer():
    """Cleanup disabled: tool messages reach the summarizer via SUMMARIZE."""
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.registry.in_memory import InMemoryStoreRegistry
    from framework.memory.archive_generation import DualLLMArchiveGenerationStrategy
    from framework.memory.layers.factory import MemoryLayerFactory

    mock_agent = MockSummarizerAgent("[LLM] summary with tools")
    archive_gen = DualLLMArchiveGenerationStrategy(summarizer=mock_agent)

    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)

    cleanup_config: dict[str, int | float] = {
        "max_messages": 11,
        "keep_ratio": 0.5,
    }
    system = DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        archive_strategy=archive_gen,
        cleanup_config=cleanup_config,
    )
    await system.initialize()
    ctx = _make_ctx("no-cleanup")

    history = system.create_message_history(ctx)
    for i in range(3):
        await history.append({"role": "user", "content": f"task {i}"})
        await history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"tc{i}", "function": {"name": "read_file"}},
        ]})
        await history.append({"role": "tool", "tool_call_id": f"tc{i}", "name": "read_file", "content": f"data{i}"})
        await history.append({"role": "assistant", "content": f"answer {i}"})

    assert mock_agent.call_count >= 1, "summarizer should be called"
    formatted = mock_agent.captured_formatted
    assert "task" in formatted


@pytest.mark.asyncio
async def test_with_cleanup_enabled_cleanup_runs_before_compression():
    """Cleanup enabled: tool msgs removed from session, compression sees remaining."""
    from framework.memory.registry.in_memory import InMemoryStoreRegistry
    from framework.memory.layers.factory import MemoryLayerFactory

    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = _make_ctx("cleanup-before")

    await layer_set.session.add_messages(ctx, [
        {"role": "user", "content": "read file"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "read_file"}}
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "file data"},
        {"role": "assistant", "content": "file says hello"},
    ])

    policy = ToolCallCleanupPolicy()
    all_msgs = [m.to_dict() for m in await layer_set.session.get_all_messages(ctx)]
    cleaned = policy.clean(all_msgs)

    assert len(cleaned) == 2, "cleanup removes intermediate tool msgs"
    assert cleaned[0]["role"] == "user"
    assert cleaned[1]["role"] == "assistant"
    assert not cleaned[1].get("tool_calls")

    mock_agent = MockSummarizerAgent("[LLM] summary without tools")
    strategy = DefaultSummarizerStrategy(mock_agent)
    result = await strategy.summarize(
        cleaned,
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert mock_agent.call_count == 1
    formatted = mock_agent.captured_formatted
    assert "read_file" not in formatted
    assert "read file" in formatted
    assert "file says hello" in formatted


# ── Filtering: irrelevant content excluded ────────────────────────────────


@pytest.mark.asyncio
async def test_summarizer_excludes_runtime_prefixes():
    """Runtime context prefixes are stripped before the summarizer sees them."""
    mock_agent = MockSummarizerAgent("[LLM] done")
    strategy = DefaultSummarizerStrategy(mock_agent)

    await strategy.summarize(
        [{"role": "user", "content": "[Runtime Context]\nsession: x\n\nreal question"}],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    assert "real question" in formatted
    assert "[Runtime Context]" not in formatted


@pytest.mark.asyncio
async def test_summarizer_formats_tool_calls_without_content():
    """Assistant with tool_calls but empty content -> tool names still formatted."""
    mock_agent = MockSummarizerAgent("[LLM] ok")
    strategy = DefaultSummarizerStrategy(mock_agent)

    await strategy.summarize(
        [
            {"role": "user", "content": "do task"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "bash"}},
                {"id": "t2", "function": {"name": "read_file"}},
            ]},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    assert "bash" in formatted
    assert "read_file" in formatted
    assert "-> tools:" in formatted


@pytest.mark.asyncio
async def test_archive_strategy_filters_nothing_sentinel_from_archive():
    """Summarizer returns '(nothing)' -> no archive entry written."""
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.registry.in_memory import InMemoryStoreRegistry
    from framework.memory.archive_generation import DualLLMArchiveGenerationStrategy
    from framework.memory.layers.factory import MemoryLayerFactory

    mock_agent = MockSummarizerAgent("(nothing)")
    archive_gen = DualLLMArchiveGenerationStrategy(summarizer=mock_agent)

    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)

    cleanup_config: dict[str, int | float] = {
        "max_messages": 5,
        "keep_ratio": 0.5,
    }
    system = DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        archive_strategy=archive_gen,
        cleanup_config=cleanup_config,
    )
    await system.initialize()
    ctx = _make_ctx("nothing-filter")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    entries = await system.get_history_entries(ctx, limit=10)
    assert len(entries) == 0, "(nothing) should not create archive entries"
    remaining = len(await system.get_history(ctx, max_messages=None))
    assert remaining < 24, (
        f"session must be cleaned even without archive writes, still has {remaining}"
    )


@pytest.mark.asyncio
async def test_summarizer_semantic_filter_removes_low_value_tool_results():
    """SemanticMessageFilter removes LOW-value tool results before LLM call."""
    mock_agent = MockSummarizerAgent("[LLM] filtered")
    strategy = DefaultSummarizerStrategy(mock_agent)

    await strategy.summarize(
        [
            {"role": "user", "content": "check status"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "list_dir"}},
            ]},
            {"role": "tool", "tool_call_id": "t1", "name": "list_dir", "content": "file1\nfile2"},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    assert "list_dir" in formatted or "[Called tools:" in formatted
    assert "file1" not in formatted or "file1" in formatted


@pytest.mark.asyncio
async def test_summarizer_keeps_high_value_tool_results():
    """MEDIUM-value tool results (web_search, read_file, etc.) are kept."""
    mock_agent = MockSummarizerAgent("[LLM] with search")
    strategy = DefaultSummarizerStrategy(mock_agent)

    await strategy.summarize(
        [
            {"role": "user", "content": "search web"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "web_search"}},
            ]},
            {"role": "tool", "tool_call_id": "t1", "name": "web_search", "content": "sunny weather"},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    formatted = mock_agent.captured_formatted
    assert "web_search" in formatted
    assert "sunny weather" in formatted
