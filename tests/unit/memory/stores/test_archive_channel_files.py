from __future__ import annotations

from framework.memory.archive_models import ArchiveChannel, ArchiveWrite
from framework.memory.core.scope import MemoryContext, MemoryLayerName
from framework.memory.layers.archive import ScopedArchiveMemoryManager
from framework.memory.layers.config import ArchiveMemoryConfig
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.file import DefaultMemoryStoreRegistry


async def test_file_registry_writes_context_and_knowledge_archive_files(tmp_path) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    await registry.initialize()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="default")

    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="context"),
        ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary="knowledge"),
    ))

    archive_root = tmp_path / "archive" / "default"
    assert (archive_root / "context_archive.jsonl").exists()
    assert (archive_root / "knowledge_archive.jsonl").exists()
    assert (archive_root / ".archive_state.json").exists()
    assert "context" in (archive_root / "context_archive.jsonl").read_text(encoding="utf-8")
    assert "knowledge" in (archive_root / "knowledge_archive.jsonl").read_text(encoding="utf-8")
