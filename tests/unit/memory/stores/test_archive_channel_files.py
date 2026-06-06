from __future__ import annotations

from framework.memory.archive_models import ArchiveChannel, ArchiveWrite
from framework.memory.core.scope import MemoryContext, MemoryLayerName
from framework.memory.layers.archive import ScopedArchiveMemoryManager
from framework.memory.layers.config import ArchiveMemoryConfig
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.file import DefaultMemoryStoreRegistry


async def test_file_registry_writes_context_and_knowledge_archive_files(tmp_path) -> None:
    """ARCHIVE layer resolves to DirArchiveStorage — append_bundle writes MD files."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    await registry.initialize()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="default")

    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="context"),
        ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary="knowledge"),
    ))

    # DirArchiveStorage writes content as {archive_id}/{channel}.md
    archive_root = tmp_path / "archive" / "default"
    assert (archive_root / "1" / "context.md").exists()
    assert (archive_root / "1" / "knowledge.md").exists()
    assert (archive_root / "state.json").exists()
    assert "context" in (archive_root / "1" / "context.md").read_text(encoding="utf-8")
    assert "knowledge" in (archive_root / "1" / "knowledge.md").read_text(encoding="utf-8")

    # No JSONL files in MD-only architecture
    assert not (archive_root / "context_archive.jsonl").exists()
    assert not (archive_root / "knowledge_archive.jsonl").exists()


async def test_append_channel_log_uses_archive_id_as_cursor() -> None:
    """Storage-level: cursor must equal archive_id from entry payload.

    Per-channel sequential counters (1,2,3...) are a V1 holdover.  With V2,
    the archive_id is the single global coordinate — two entries written to
    the same channel with archive_ids 7 and 3 MUST have cursors 7 and 3,
    NOT per-channel counters 1 and 2.
    """
    from framework.memory.stores.scoped_in_memory import InMemoryScopedStorage

    storage = InMemoryScopedStorage()
    await storage.initialize()

    r1 = await storage.append_channel_log(
        "context", {"archive_id": 7, "summary": "first", "entry_id": 7},
    )
    assert r1["cursor"] == 7, (
        f"Expected cursor=7 (archive_id), got {r1['cursor']} (per-channel counter)"
    )

    r2 = await storage.append_channel_log(
        "context", {"archive_id": 3, "summary": "second", "entry_id": 3},
    )
    assert r2["cursor"] == 3, (
        f"Expected cursor=3 (archive_id), got {r2['cursor']} (per-channel counter)"
    )
