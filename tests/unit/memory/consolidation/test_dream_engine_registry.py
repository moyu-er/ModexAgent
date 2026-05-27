from __future__ import annotations

from framework.memory.archive_models import KNOWLEDGE_ARCHIVE_FILE_KEY, ArchiveChannel
from framework.memory.consolidation.dream_engine import DreamEngine
from framework.memory.core.models import ArchiveEntry, LongTermMemory, UnprocessedResult
from framework.memory.core.scope import MemoryAgentRole, MemoryContext, MemoryLayerName, ScopeRecord


class DummyLLM:
    async def chat_with_retry(self, **kwargs):
        return "[SKIP] no new information"


class DummyArchiveManager:
    def __init__(self):
        self.seen_contexts = []
        self.committed = []
        self.pruned_contexts = []
        self.unprocessed_channels = []

    def __init__(self, entry_count: int = 6):
        self.seen_contexts = []
        self.committed = []
        self.pruned_contexts = []
        self.unprocessed_channels = []
        self._entry_count = entry_count

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
    engine = DreamEngine(
        llm_provider=DummyLLM(),
        history_manager=archive,
        long_term_manager=DummyKnowledgeManager(),
        registry=registry,
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
    assert archive.committed == [(context, "dream", 6, ArchiveChannel.KNOWLEDGE)]
    assert archive.pruned_contexts == [context]
