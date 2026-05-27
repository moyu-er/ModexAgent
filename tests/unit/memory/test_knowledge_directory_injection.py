"""Tests for knowledge directory path injection."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from framework.memory.core.models import LongTermMemory
from framework.memory.core.scope import MemoryContext
from framework.memory.core.system import InjectableMemorySystem
from framework.memory.injection.full_injection import FullInjectionPolicy


def _make_injectable_system(knowledge_dir: Path | None = None):
    """Create a mock InjectableMemorySystem."""
    system = AsyncMock(spec=InjectableMemorySystem)
    system.retrieve_knowledge = AsyncMock(return_value=LongTermMemory(
        soul="I am a test assistant.",
        user="- **Name**: (unknown)",
        memory="# Memory\n(empty)",
        custom={},
    ))
    system.get_knowledge_directory = AsyncMock(return_value=knowledge_dir)
    system.get_history = AsyncMock(return_value=[])
    system.get_history_entries = AsyncMock(return_value=[])
    system.get_providers = MagicMock(return_value=[])
    system.prefetch_memories = AsyncMock(return_value=None)
    system.create_message_history = MagicMock(return_value=[])
    return system


@pytest.mark.asyncio
async def test_injects_knowledge_directory_path(tmp_path):
    """Should inject knowledge directory path as a PromptSection."""
    policy = FullInjectionPolicy()
    context = MemoryContext(session_id="s1", user_id="u1")
    system = _make_injectable_system(knowledge_dir=tmp_path.resolve())

    bundle = await policy.assemble(context=context, memory_system=system)

    dir_section = next((s for s in bundle.system_sections if s.key == "knowledge:directory"), None)
    assert dir_section is not None
    assert dir_section.priority == 95
    assert str(tmp_path.resolve()) in dir_section.content
    assert "SOUL.md" in dir_section.content
    assert "USER.md" in dir_section.content
    assert "MEMORY.md" in dir_section.content


@pytest.mark.asyncio
async def test_no_directory_section_when_knowledge_disabled():
    """Should not inject directory section when get_knowledge_directory returns None."""
    policy = FullInjectionPolicy()
    context = MemoryContext(session_id="s1", user_id="u1")
    system = _make_injectable_system(knowledge_dir=None)

    bundle = await policy.assemble(context=context, memory_system=system)

    dir_section = next((s for s in bundle.system_sections if s.key == "knowledge:directory"), None)
    assert dir_section is None


@pytest.mark.asyncio
async def test_directory_section_cross_platform_path(tmp_path):
    """Should use resolve() for absolute path regardless of platform."""
    policy = FullInjectionPolicy()
    context = MemoryContext(session_id="s1", user_id="u1")

    # Use a path with subdirectories
    deep_dir = tmp_path / "data" / "memory" / "main" / "knowledge" / "default"
    deep_dir.mkdir(parents=True, exist_ok=True)

    system = _make_injectable_system(knowledge_dir=deep_dir.resolve())

    bundle = await policy.assemble(context=context, memory_system=system)

    dir_section = next((s for s in bundle.system_sections if s.key == "knowledge:directory"), None)
    assert dir_section is not None
    # Verify it's an absolute path
    injected_path = Path(dir_section.content.split("`")[1])
    assert injected_path.is_absolute()


def test_scoped_storage_base_path(tmp_path):
    from framework.memory.stores.scoped_file import DefaultScopedStorage
    from framework.memory.core.scope import MemoryLayerName

    storage = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.KNOWLEDGE)
    result = storage.base_path
    assert result is not None
    assert result.is_absolute()
    assert result == tmp_path.resolve()


def test_in_memory_storage_base_path():
    from framework.memory.stores.scoped_in_memory import InMemoryScopedStorage

    storage = InMemoryScopedStorage()
    assert storage.base_path is None
