"""Tests for ArchiveStrategy implementations."""

import json

import pytest

from framework.memory.archive import (
    PreserveSummaryArchiveStrategy,
    RawDumpArchiveStrategy,
    SemanticArchiveStrategy,
)
from framework.memory.core.compression import CompressionResult
from framework.memory.core.scope import MemoryContext, UserScope
from framework.memory.managers.history import HistoryArchiveManager
from framework.memory.stores.in_memory import InMemoryStorage


@pytest.fixture
async def storage():
    s = InMemoryStorage()
    await s.initialize()
    yield s
    await s.close()


@pytest.mark.asyncio
class TestPreserveSummaryArchiveStrategy:
    async def test_uses_summary_when_present(self, storage):
        history = HistoryArchiveManager(storage, UserScope())
        strategy = PreserveSummaryArchiveStrategy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        result = CompressionResult(
            summary="LLM generated summary",
            pruned_messages=[{"role": "user", "content": "hi"}],
        )
        await strategy.archive(ctx, result.pruned_messages, result, history)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        assert entries[0]["summary"] == "LLM generated summary"
        assert entries[0]["metadata"]["source"] == "compression_summary"

    async def test_falls_back_to_raw_dump_when_summary_empty(self, storage):
        history = HistoryArchiveManager(storage, UserScope())
        strategy = PreserveSummaryArchiveStrategy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        result = CompressionResult(
            summary="",
            pruned_messages=[{"role": "user", "content": "hello"}],
        )
        await strategy.archive(ctx, result.pruned_messages, result, history)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        assert "hello" in entries[0]["summary"]
        assert entries[0]["metadata"]["source"] == "raw_dump_fallback"

    async def test_raw_dump_strips_runtime_prefix(self, storage):
        history = HistoryArchiveManager(storage, UserScope())
        strategy = PreserveSummaryArchiveStrategy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        result = CompressionResult(
            summary="",
            pruned_messages=[
                {
                    "role": "user",
                    "content": "[Runtime Context]\nchannel=qq\n\nActual question",
                }
            ],
        )
        await strategy.archive(ctx, result.pruned_messages, result, history)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        assert "[Runtime Context]" not in entries[0]["summary"]
        assert "Actual question" in entries[0]["summary"]
        assert entries[0]["metadata"]["source"] == "raw_dump_fallback"


@pytest.mark.asyncio
class TestRawDumpArchiveStrategy:
    async def test_always_dumps_raw_messages(self, storage):
        history = HistoryArchiveManager(storage, UserScope())
        strategy = RawDumpArchiveStrategy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        result = CompressionResult(
            summary="ignored summary",
            pruned_messages=[
                {"role": "user", "content": "msg1"},
                {"role": "assistant", "content": "msg2"},
            ],
        )
        await strategy.archive(ctx, result.pruned_messages, result, history)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        assert "msg1" in entries[0]["summary"]
        assert "msg2" in entries[0]["summary"]
        assert entries[0]["metadata"]["source"] == "raw_dump"
        assert "ignored summary" not in entries[0]["summary"]

    async def test_handles_tool_calls_in_raw_dump(self, storage):
        history = HistoryArchiveManager(storage, UserScope())
        strategy = RawDumpArchiveStrategy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        result = CompressionResult(
            summary="",
            pruned_messages=[
                {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
            ],
        )
        await strategy.archive(ctx, result.pruned_messages, result, history)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        assert "[tool_calls]" in entries[0]["summary"]


@pytest.mark.asyncio
class TestSemanticArchiveStrategy:
    async def test_uses_llm_summary_when_available(self, storage):
        """spec:archive-entry 'LLM summary available'"""
        history = HistoryArchiveManager(storage, UserScope())
        strategy = SemanticArchiveStrategy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        result = CompressionResult(
            summary="User asked about weather",
            pruned_messages=[{"role": "user", "content": "What's the weather?"}],
        )
        await strategy.archive(ctx, result.pruned_messages, result, history)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        assert entries[0]["summary"] == "User asked about weather"
        assert entries[0]["metadata"]["source"] == "compression_summary"

    async def test_falls_back_to_sanitized_fallback(self, storage):
        """spec:archive-entry 'No LLM summary with semantic messages'"""
        history = HistoryArchiveManager(storage, UserScope())
        strategy = SemanticArchiveStrategy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        result = CompressionResult(
            summary="",
            pruned_messages=[
                {"role": "user", "content": "Tell me a joke."},
                {"role": "assistant", "content": "Why did the chicken..."},
            ],
        )
        await strategy.archive(ctx, result.pruned_messages, result, history)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        assert entries[0]["metadata"]["source"] == "sanitized_fallback"
        assert " Tell me a joke." in entries[0]["summary"] or "Tell me a joke" in entries[0]["summary"]

    async def test_handles_empty_sanitized_output(self, storage):
        """spec:archive-entry 'All pruned messages are LOW-value'"""
        history = HistoryArchiveManager(storage, UserScope())
        strategy = SemanticArchiveStrategy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        result = CompressionResult(
            summary="",
            pruned_messages=[
                # 一条孤立的 LOW tool 消息会被 sanitize 直接丢弃，导致空列表
                {"role": "tool", "content": "big output", "tool_call_id": "tc1", "name": "shell"},
            ],
        )
        await strategy.archive(ctx, result.pruned_messages, result, history)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        assert entries[0]["summary"] == "(no semantic content)"
        assert entries[0]["metadata"]["source"] == "empty"

    async def test_never_emits_raw_tool_dumps(self, storage):
        """spec:archive-entry 'Pruned messages contain tool dumps'"""
        history = HistoryArchiveManager(storage, UserScope())
        strategy = SemanticArchiveStrategy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        big_json = json.dumps({"data": "x" * 1000})
        result = CompressionResult(
            summary="",
            pruned_messages=[
                # 工具调用链被整体折叠或丢弃后，raw json 不应出现在 summary 中
                {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "shell"}}]},
                {"role": "tool", "content": big_json, "tool_call_id": "tc1", "name": "shell"},
            ],
        )
        await strategy.archive(ctx, result.pruned_messages, result, history)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        # raw json should not appear verbatim in summary
        assert big_json not in entries[0]["summary"]

    async def test_metadata_contains_traceability_fields(self, storage):
        history = HistoryArchiveManager(storage, UserScope())
        strategy = SemanticArchiveStrategy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        result = CompressionResult(
            summary="summary",
            pruned_messages=[{"role": "user", "content": "hi"}],
        )
        await strategy.archive(ctx, result.pruned_messages, result, history)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        meta = entries[0]["metadata"]
        assert "pruned_count" in meta
        assert "semantic_count" in meta
        assert "source" in meta
