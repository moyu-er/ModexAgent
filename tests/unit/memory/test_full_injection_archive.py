"""Tests for FullInjectionPolicy archive injection."""
from __future__ import annotations

import pytest

from framework.memory.core.scope import MemoryContext
from framework.memory.core.system import InjectableMemorySystem
from framework.memory.injection.full_injection import FullInjectionPolicy
from framework.memory.stores.dir_archive import DirArchiveStorage
from framework.memory.tags import ArchiveTag


class _FakeInjectableMemorySystem(InjectableMemorySystem):
    """Minimal injectable memory system for testing archive injection."""

    def __init__(self, archive_dir):
        self._archive_dir = archive_dir

    async def get_storage_path(self, context):
        return self._archive_dir

    async def get_history(self, context, max_messages=None):
        return []

    async def retrieve_knowledge(self, context, query=""):
        from framework.memory.core.models import LongTermMemory
        return LongTermMemory()

    async def get_history_entries(self, context, limit=3, query="", channel=None):
        return []

    async def get_knowledge_directory(self, context):
        return None

    def get_providers(self):
        return []

    async def prefetch_memories(self, query, context):
        return None


@pytest.mark.asyncio
async def test_inject_md_archives_truncates_at_configured_chars(tmp_path):
    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)

    long_content = "A" * 1200
    await storage.write_archive_file(1, "context.md", long_content)

    fake = _FakeInjectableMemorySystem(archive_dir)
    policy = FullInjectionPolicy(
        archive_inject_count=3,
        archive_inject_max_chars=1000,
    )

    result = await policy.assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )

    assert ArchiveTag.CONTAINER.value in result.system_prompt
    assert "A" * 1000 in result.system_prompt
    assert "..." in result.system_prompt
    assert "A" * 1100 not in result.system_prompt


@pytest.mark.asyncio
async def test_inject_md_archives_ascending_order(tmp_path):
    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)

    await storage.write_archive_file(1, "context.md", "first archive")
    await storage.write_archive_file(2, "context.md", "second archive")
    await storage.write_archive_file(3, "context.md", "third archive")

    fake = _FakeInjectableMemorySystem(archive_dir)
    policy = FullInjectionPolicy(
        archive_inject_count=3,
        archive_inject_max_chars=1000,
    )

    result = await policy.assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )

    first_pos = result.system_prompt.find('number="1"')
    second_pos = result.system_prompt.find('number="2"')
    third_pos = result.system_prompt.find('number="3"')
    assert first_pos < second_pos < third_pos


@pytest.mark.asyncio
async def test_inject_md_archives_respects_count_limit(tmp_path):
    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)

    for aid in [1, 2, 3, 4, 5]:
        await storage.write_archive_file(aid, "context.md", f"archive {aid}")

    fake = _FakeInjectableMemorySystem(archive_dir)
    policy = FullInjectionPolicy(
        archive_inject_count=2,
        archive_inject_max_chars=1000,
    )

    result = await policy.assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )

    assert 'number="4"' in result.system_prompt
    assert 'number="5"' in result.system_prompt
    assert 'number="3"' not in result.system_prompt
    assert 'number="1"' not in result.system_prompt


@pytest.mark.asyncio
async def test_inject_md_archives_skips_empty_context(tmp_path):
    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)

    await storage.write_archive_file(1, "context.md", "valid content")
    await storage.write_archive_file(2, "context.md", "")

    fake = _FakeInjectableMemorySystem(archive_dir)
    policy = FullInjectionPolicy(
        archive_inject_count=3,
        archive_inject_max_chars=1000,
    )

    result = await policy.assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )

    assert 'number="1"' in result.system_prompt
    assert 'number="2"' not in result.system_prompt
