from __future__ import annotations

from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.memory.archive_models import KNOWLEDGE_ARCHIVE_FILE_KEY, ArchiveChannel
from framework.memory.consolidation.dream_engine import DreamEngine, _file_needs_update
from framework.memory.core.models import ArchiveEntry, LongTermMemory, UnprocessedResult
from framework.memory.core.scope import MemoryAgentRole, MemoryContext, MemoryLayerName, ScopeRecord
from framework.memory.prompts import PromptRegistry


class _FakePath:
    """Minimal Path stand-in for dummy managers."""

    def __init__(self, value: str):
        self._value = value

    def resolve(self):
        return Path(self._value)

    def __str__(self):
        return self._value


class DummyLLM:
    async def chat_with_retry(self, **kwargs):
        return "[SKIP] no new information"


class DummyArchiveManager:
    def __init__(self, entry_count: int = 6):
        self.seen_contexts = []
        self.committed = []
        self.pruned_contexts = []
        self.unprocessed_channels = []
        self._entry_count = entry_count

    async def get_storage_path(self, context):
        return _FakePath("/tmp/archive")

    async def get_unprocessed(self, context, cursor_name, limit=100, *, channel=ArchiveChannel.KNOWLEDGE):
        self.seen_contexts.append(context)
        self.unprocessed_channels.append(channel)
        return UnprocessedResult(
            cursor=self._entry_count,
            entries=[
                ArchiveEntry(summary=f"summary {i}", entry_id=i + 1)
                for i in range(self._entry_count)
            ],
        )

    async def commit_cursor(self, context, cursor_name, cursor, *, channel=ArchiveChannel.KNOWLEDGE):
        self.committed.append((context, cursor_name, cursor, channel))

    async def prune_consumed_pairs(self, context):
        self.pruned_contexts.append(context)


class DummyKnowledgeManager:
    async def get_storage_path(self, context):
        return _FakePath("/tmp/knowledge")

    async def get_all(self, context):
        return LongTermMemory()

    async def apply_update(self, context, update):
        return update.content


class DummyRegistry:
    def __init__(self, records):
        self.records = records
        self.calls = []

    async def list_records(self, **kwargs):
        self.calls.append(kwargs)
        return self.records


async def test_dream_engine_scan_all_uses_registry_records() -> None:
    context = MemoryContext(session_id="s1", user_id="u1")
    registry = DummyRegistry(
        [
            ScopeRecord(
                scope_key="u1",
                layer=MemoryLayerName.ARCHIVE,
                context=context,
                storage_path="memory://archive/u1",
                agent_role=MemoryAgentRole.MAIN,
            )
        ]
    )
    archive = DummyArchiveManager()
    mock_consolidator = AsyncMock()
    mock_consolidator.consolidate.return_value = True
    engine = DreamEngine(
        llm_provider=DummyLLM(),
        history_manager=archive,
        long_term_manager=DummyKnowledgeManager(),
        registry=registry,
        consolidator=mock_consolidator,
    )

    processed = await engine.scan_all()

    assert processed == [context]
    assert registry.calls == [
        {
            "layer": MemoryLayerName.ARCHIVE,
            "has_file": KNOWLEDGE_ARCHIVE_FILE_KEY,
            "agent_roles": {MemoryAgentRole.MAIN},
        }
    ]
    assert archive.unprocessed_channels == [ArchiveChannel.KNOWLEDGE]
    assert len(archive.committed) == 1
    assert archive.committed[0][0] == context
    assert archive.committed[0][1] == "dream"
    assert archive.committed[0][2] == 3  # max_consume_per_run=3 limits to 3 entries
    assert archive.committed[0][3] == ArchiveChannel.KNOWLEDGE
    assert archive.pruned_contexts == [context]


def test_file_needs_update_detects_marker() -> None:
    """_file_needs_update detects [FILE] markers in analysis text."""
    assert _file_needs_update("[SOUL] new principle", "soul") is True
    assert _file_needs_update("[USER] name is Alice", "user") is True
    assert _file_needs_update("[MEMORY] project info", "memory") is True
    assert _file_needs_update("[SKIP] nothing", "soul") is False
    assert _file_needs_update("no markers here", "soul") is False


@pytest.mark.asyncio
async def test_dream_engine_accepts_prompts_parameter() -> None:
    """DreamEngine accepts optional prompts parameter and uses per-file Phase 2."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prompts_dir = Path(tmpdir)
        (prompts_dir / "knowledge").mkdir()
        (prompts_dir / "knowledge" / "soul_update_system.md").write_text("SOUL system")
        (prompts_dir / "knowledge" / "soul_update_user.md").write_text("SOUL user: {current_soul}")
        (prompts_dir / "knowledge" / "fact_extraction_system.md").write_text("FACT system")
        (prompts_dir / "knowledge" / "fact_extraction_user.md").write_text("FACT user: {archive_entries}")

        registry = PromptRegistry(prompts_dir)

        summarizer = MagicMock()
        summarizer.analyze = AsyncMock(return_value="[SOUL] new principle")
        summarizer.summarize = AsyncMock(
            return_value='[{"file_name": "SOUL.md", "content": "new", "reason": "test"}]'
        )

        engine = DreamEngine(
            llm_provider=MagicMock(),
            history_manager=MagicMock(),
            long_term_manager=MagicMock(),
            summarizer=summarizer,
            prompts=registry,
        )

        result = await engine.consolidate(
            scope_key="",
            new_entries=[{"summary": "test entry"}],
            existing_memories={
                "SOUL.md": "I am helpful",
                "USER.md": "Name: Alice",
                "MEMORY.md": "Project: ModexAgent",
            },
        )

        assert result.success is True
        assert summarizer.summarize.call_count >= 1


@pytest.mark.asyncio
async def test_per_file_update_uses_raw_output_not_json() -> None:
    """Per-file Phase 2 uses raw LLM output directly, not _parse_updates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prompts_dir = Path(tmpdir)
        (prompts_dir / "knowledge").mkdir()
        (prompts_dir / "knowledge" / "soul_update_system.md").write_text("SOUL system")
        (prompts_dir / "knowledge" / "soul_update_user.md").write_text("SOUL user: {current_soul}")
        (prompts_dir / "knowledge" / "fact_extraction_system.md").write_text("FACT system")
        (prompts_dir / "knowledge" / "fact_extraction_user.md").write_text("FACT user")

        registry = PromptRegistry(prompts_dir)

        summarizer = MagicMock()
        summarizer.analyze = AsyncMock(return_value="[SOUL] new principle\n[USER] name is Bob")
        raw_soul_content = "# Soul Profile\n\nI am helpful.\n\n## Core Principles\n- Be concise"
        summarizer.summarize = AsyncMock(return_value=raw_soul_content)

        engine = DreamEngine(
            llm_provider=MagicMock(),
            history_manager=MagicMock(),
            long_term_manager=MagicMock(),
            summarizer=summarizer,
            prompts=registry,
        )

        engine.long_term_manager.get_all = AsyncMock()
        mock_knowledge = MagicMock()
        mock_knowledge.soul = "old content"
        mock_knowledge.user = "old user"
        mock_knowledge.memory = "old memory"
        engine.long_term_manager.get_all.return_value = mock_knowledge

        result = await engine.consolidate(
            scope_key="",
            new_entries=[{"summary": "test"}],
            existing_memories={
                "SOUL.md": "old content",
                "USER.md": "old user",
                "MEMORY.md": "old memory",
            },
        )

        assert result.success is True
        assert len(result.soul_updates) == 1
        assert result.soul_updates[0].content == raw_soul_content
        assert result.soul_updates[0].file_name == "SOUL.md"
