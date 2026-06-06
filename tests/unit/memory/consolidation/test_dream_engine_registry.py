from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.memory.archive_models import KNOWLEDGE_ARCHIVE_FILE_KEY, ArchiveChannel
from framework.memory.consolidation.dream_engine import DreamEngine
from framework.memory.core.models import ArchiveEntry, LongTermMemory, UnprocessedResult
from framework.memory.core.scope import MemoryAgentRole, MemoryContext, MemoryLayerName, ScopeRecord


class _FakePath:
    """Minimal Path stand-in for dummy managers."""

    def __init__(self, value: str):
        self._value = value

    def resolve(self):
        return Path(self._value)

    def __str__(self):
        return self._value


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


@pytest.mark.asyncio
async def test_consolidate_raises_not_implemented() -> None:
    """consolidate() raises NotImplementedError — legacy path removed."""
    engine = DreamEngine(
        history_manager=MagicMock(),
        long_term_manager=MagicMock(),
    )
    with pytest.raises(NotImplementedError, match="has been removed"):
        await engine.consolidate(
            scope_key="",
            new_entries=[{"summary": "test"}],
            existing_memories={},
        )
