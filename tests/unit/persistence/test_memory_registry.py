from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from modex_agent.agents.summarizer.abc import (
    ArchiveGenerator,
)
from modex_agent.core.scope import (
    MemoryContext,
    MemoryLayerName,
    RecordScope,
    SessionScope,
    UserScope,
)
from modex_agent.memory.archive_models import (
    ArchiveDocuments,
    ArchiveGenerationResult,
)
from modex_agent.memory.cleanup import cleanup_session
from modex_agent.memory.core.models import ArchiveEntry
from modex_agent.memory.layers.config import ArchiveMemoryConfig, MemoryLayerConfigSet
from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.memory.stores.dir_archive import DirArchiveStorage
from modex_agent.memory.stores.markdown_knowledge import MarkdownKnowledgeStorage
from modex_agent.memory.system import MemorySystemContextManager, create_memory_system
from modex_agent.memory.token_estimator import TokenEstimator
from modex_agent.persistence.adapters.archive_store import SqliteArchiveStore
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.persistence.memory_registry import HybridMemoryStoreRegistry


class _PoolScopedRecordScope(RecordScope):
    """Test-only RecordScope subclass with pool dimension (ADR-0028)."""

    pool: str | None = None


class _FixedEstimator(TokenEstimator):
    def estimate_text(self, text: str) -> int:
        _ = text
        return 10


class _TypedArchiveGenerator(ArchiveGenerator):
    async def generate(
        self,
        pruned_messages: Sequence[dict[str, Any]],
    ) -> ArchiveGenerationResult:
        _ = pruned_messages
        documents = ArchiveDocuments(
            context="SQLite cleanup context",
            knowledge="SQLite cleanup knowledge",
            index="SQLite Cleanup Topic",
        )
        return ArchiveGenerationResult(
            documents=documents,
        )


def test_memory_system_defaults_to_file_registry(tmp_path: Path) -> None:
    system = create_memory_system(tmp_path)

    assert isinstance(system.store_registry, DefaultMemoryStoreRegistry)


async def test_hybrid_registry_routes_structured_stores_and_keeps_documents_on_disk(
    tmp_path: Path,
) -> None:
    persistence = WorkspacePersistenceManager(tmp_path / "state.db")
    await persistence.open()
    registry = HybridMemoryStoreRegistry(
        file_root=tmp_path / "memory",
        persistence=persistence,
        base_scope=_PoolScopedRecordScope(pool="main", workspace_id="workspace-1"),
    )
    await registry.initialize()
    context = MemoryContext(session_id="session-1", user_id="user-1")
    try:
        session = await registry.resolve(
            layer=MemoryLayerName.SESSION,
            scope=SessionScope(),
            context=context,
        )
        archive = await registry.resolve(
            layer=MemoryLayerName.ARCHIVE,
            scope=UserScope(),
            context=context,
        )
        knowledge = await registry.resolve(
            layer=MemoryLayerName.KNOWLEDGE,
            scope=UserScope(),
            context=context,
        )

        await session.messages.append_message({"id": "m1", "role": "user", "content": "hello"})
        assert isinstance(archive.messages, DirArchiveStorage)
        assert isinstance(archive.archive, SqliteArchiveStore)
        assert isinstance(knowledge.messages, MarkdownKnowledgeStorage)
        await knowledge.kv.set("MEMORY.md", "file-backed knowledge")
        archive_directory = archive.messages.directory
        archive_document = archive_directory / "1" / "context.md"
        archive_document.parent.mkdir(parents=True)
        archive_document.write_text("file-backed archive", encoding="utf-8")
        assert archive.archive is not None
        await archive.archive.write_archive_state({"next_archive_id": 2})

        assert (knowledge.messages.directory / "MEMORY.md").read_text(
            encoding="utf-8"
        ) == "file-backed knowledge"
        assert archive_document.read_text(encoding="utf-8") == "file-backed archive"
        assert (
            await persistence.connection.query_value(
                "SELECT COUNT(*) FROM memory_session_messages",
                int,
            )
            == 1
        )
        assert (
            await persistence.connection.query_value(
                "SELECT COUNT(*) FROM memory_archive_state",
                int,
            )
            == 1
        )
    finally:
        await registry.close()

    assert (
        await persistence.connection.query_value(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'",
            int,
        )
        > 0
    )
    await persistence.close()


async def test_hybrid_archive_prompt_omits_file_path_metadata(
    tmp_path: Path,
) -> None:
    persistence = WorkspacePersistenceManager(tmp_path / "state.db")
    await persistence.open()
    registry = HybridMemoryStoreRegistry(
        file_root=tmp_path / "memory",
        persistence=persistence,
        base_scope=_PoolScopedRecordScope(pool="main", workspace_id="workspace-1"),
    )
    system = create_memory_system(
        tmp_path / "memory",
        config=MemoryLayerConfigSet(archive=ArchiveMemoryConfig()),
        store_registry=registry,
    )
    await system.initialize()
    try:
        context = MemoryContext(session_id="session-1", user_id="default")
        archive = system.layers.archive
        assert archive is not None
        await archive.append(
            context,
            ArchiveEntry(summary="SQLite-backed archive summary"),
        )
        manager = MemorySystemContextManager(system)
        state = await manager.load("session-1")
        assert state.system_prompt_pipeline is not None
        prompt = await state.system_prompt_pipeline.get_or_refresh()

        assert "SQLite-backed archive summary" in prompt
        assert "file=" not in prompt
        assert "context.md" not in prompt
        assert "state.db" not in prompt
    finally:
        await system.close()
        await persistence.close()


async def test_sqlite_cleanup_commits_generated_archive_and_injects_context(
    tmp_path: Path,
) -> None:
    persistence = WorkspacePersistenceManager(tmp_path / "state.db")
    await persistence.open()
    registry = HybridMemoryStoreRegistry(
        file_root=tmp_path / "memory",
        persistence=persistence,
        base_scope=_PoolScopedRecordScope(pool="main", workspace_id="workspace-1"),
    )
    system = create_memory_system(
        tmp_path / "memory",
        config=MemoryLayerConfigSet(archive=ArchiveMemoryConfig()),
        store_registry=registry,
    )
    await system.initialize()
    context = MemoryContext(session_id="cleanup-session", user_id="default")
    pruned = PrunedManager(pruned_base_dir=tmp_path / "pruned")
    try:
        for index in range(10):
            await system.layers.session.add_messages(
                context,
                [{"id": f"msg-{index}", "role": "user", "content": f"message-{index}"}],
            )

        result = await cleanup_session(
            session=system.layers.session,
            archive=system.layers.archive,
            context=context,
            max_context_tokens=50,
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(),
            archive_agent=_TypedArchiveGenerator(),
            pruned_manager=pruned,
        )

        assert result.archive_skipped is False
        assert result.messages_pruned > 0
        assert len(await system.layers.session.get_all_messages(context)) < 10
        assert (
            await persistence.connection.query_value(
                "SELECT COUNT(*) FROM memory_archive_entries",
                int,
            )
            == 2
        )
        state_count = await persistence.connection.query_value(
            "SELECT COUNT(*) FROM memory_archive_state WHERE next_archive_id = 2",
            int,
        )
        assert state_count == 1
        entries = await system.get_history_entries(context)
        assert [entry["summary"] for entry in entries] == ["SQLite cleanup context"]
        prompt = await MemorySystemContextManager(system).load("cleanup-session")
        assert prompt.system_prompt_pipeline is not None
        injected = await prompt.system_prompt_pipeline.get_or_refresh()
        assert "SQLite cleanup context" in injected
        assert "file=" not in injected
        assert pruned._get_storage(context.session_id or "").read_index()[-1].topic == (
            "SQLite Cleanup Topic"
        )
    finally:
        await system.close()
        await persistence.close()
