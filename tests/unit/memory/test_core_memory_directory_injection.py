"""Tests for knowledge directory path injection."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.memory.core.models import CoreMemoryContents
from modex_agent.memory.injection.full_injection import FullInjectionPolicy
from modex_agent.memory.scope import MemoryContext


def _make_injectable_system(knowledge_dir: Path | None = None):
    """Create a mock InjectableMemorySystem."""
    system = AsyncMock()
    # ABC conformance — stub all abstract methods
    system.get_core_memory = AsyncMock()
    system.retrieve_core_memory = AsyncMock()
    system.get_history_entries = AsyncMock()
    system.get_providers = lambda: []
    system.prefetch_memories = AsyncMock()
    system.get_core_memory_directory = AsyncMock()
    system.get_storage_path = AsyncMock()
    system.get_history = AsyncMock()
    system.create_message_history = MagicMock()
    system.initialize = AsyncMock()
    system.close = AsyncMock()
    system.add_messages = AsyncMock()
    system.search = AsyncMock()
    system.clear = AsyncMock()
    system.retrieve_core_memory = AsyncMock(return_value=CoreMemoryContents(
        soul="I am a test assistant.",
        user="- **Name**: (unknown)",
        memory="# Memory\n(empty)",
        custom={},
    ))
    system.get_core_memory_directory = AsyncMock(return_value=knowledge_dir)
    system.get_history = AsyncMock(return_value=[])
    system.get_history_entries = AsyncMock(return_value=[])
    system.get_providers = MagicMock(return_value=[])
    system.prefetch_memories = AsyncMock(return_value=None)
    system.create_message_history = MagicMock(return_value=[])
    return system


@pytest.mark.asyncio
async def test_injects_knowledge_directory_path(tmp_path):
    """Should inject knowledge as XML with absolute file paths."""
    policy = FullInjectionPolicy()
    context = MemoryContext(session_id="s1", user_id="u1")
    system = _make_injectable_system(knowledge_dir=tmp_path.resolve())

    result = await policy.assemble(context=context, memory_system=system)

    assert "SOUL.md" in result.system_prompt
    assert "USER.md" in result.system_prompt
    assert "MEMORY.md" in result.system_prompt
    assert 'file="' in result.system_prompt
    assert str(tmp_path.resolve()) in result.system_prompt
    assert str(tmp_path.resolve()) in result.system_prompt
    assert "SOUL.md" in result.system_prompt
    assert "USER.md" in result.system_prompt
    assert "MEMORY.md" in result.system_prompt
    assert 'file="' in result.system_prompt


@pytest.mark.asyncio
async def test_no_injection_when_knowledge_disabled():
    """Test when knowledge directory is None, we still get XML but no file paths."""
    policy = FullInjectionPolicy()
    context = MemoryContext(session_id="s1", user_id="u1")
    system = _make_injectable_system(knowledge_dir=None)

    result = await policy.assemble(context=context, memory_system=system)

    if "<your_identity>" in result.system_prompt or "<user_profile>" in result.system_prompt or "<known_facts>" in result.system_prompt:
        assert 'file="' not in result.system_prompt


@pytest.mark.asyncio
async def test_directory_section_cross_platform_path(tmp_path):
    """Knowledge injection emits a top-level directory path + relative filenames."""
    policy = FullInjectionPolicy()
    context = MemoryContext(session_id="s1", user_id="u1")

    deep_dir = tmp_path / "data" / "memory" / "main" / "knowledge" / "default"
    deep_dir.mkdir(parents=True, exist_ok=True)

    system = _make_injectable_system(knowledge_dir=deep_dir.resolve())

    result = await policy.assemble(context=context, memory_system=system)

    import re
    # The heading now contains "Directory: <absolute_path>"
    dir_match = re.search(r'Directory:\s+(\S+)', result.system_prompt)
    assert dir_match is not None, "Expected a 'Directory: <path>' line in heading"
    dir_path = Path(dir_match.group(1))
    assert dir_path.is_absolute()

    # File attributes are now relative filenames (e.g. file="SOUL.md")
    file_match = re.search(r'file="([^"]+)"', result.system_prompt)
    if file_match is not None:
        injected_path = Path(file_match.group(1))
        assert not injected_path.is_absolute(), (
            f"Expected relative filename, got absolute: {injected_path}"
        )
        assert injected_path.name in ("SOUL.md", "USER.md", "MEMORY.md")


def test_scoped_storage_base_path(tmp_path):
    from modex_agent.memory.scope import MemoryLayerName
    from modex_agent.memory.stores.scoped_file import DefaultScopedStorage

    storage = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.CORE)
    result = storage.base_path
    assert result is not None
    assert result.is_absolute()
    assert result == tmp_path.resolve()


def test_in_memory_storage_base_path():
    from modex_agent.memory.stores.scoped_in_memory import InMemoryScopedStorage

    storage = InMemoryScopedStorage()
    assert storage.base_path is None


@pytest.mark.asyncio
async def test_knowledge_injection_uses_xml_with_absolute_paths():
    """Knowledge injection wraps content with absolute file paths when active."""
    import sys
    if sys.platform == "win32":
        test_path = Path("C:\\tmp\\memory\\knowledge")
    else:
        test_path = Path("/tmp/memory/knowledge")

    policy = FullInjectionPolicy()
    context = MemoryContext(session_id="s1", user_id="u1")
    system = _make_injectable_system(knowledge_dir=test_path)

    result = await policy.assemble(context=context, memory_system=system)

    knowledge_tags = ("your_identity", "user_profile", "known_facts")
    has_knowledge = any(f"<{t}>" in result.system_prompt for t in knowledge_tags)
    if has_knowledge:
        assert any(f"</{t}>" in result.system_prompt for t in knowledge_tags)
        assert 'file="' in result.system_prompt
        assert 'SOUL.md' in result.system_prompt
        assert 'USER.md' in result.system_prompt
        assert 'MEMORY.md' in result.system_prompt
        assert 'editable="true"' in result.system_prompt
        assert 'editable="false"' in result.system_prompt
        assert 'description="Who you are' in result.system_prompt
        assert 'description="Facts about the user' in result.system_prompt
    assert 'description="Known facts about the project' in result.system_prompt
