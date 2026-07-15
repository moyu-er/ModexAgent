"""Integration tests for ArchiveProvider and PrunedProvider with real storage backends.

Uses actual DirArchiveStorage and PrunedManager (not mocks) to verify
the dynamic providers interact correctly with the storage layer.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from modex_agent.core.scope import MemoryContext
from modex_agent.memory.core.models import ArchiveEntry
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.injection.archive import ArchiveInjectionConfig
from modex_agent.memory.prompt_pipeline.providers import ArchiveProvider, PrunedProvider
from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.system import create_memory_system


async def _archive_system(tmp_path: Path) -> DefaultMemorySystem:
    system = create_memory_system(tmp_path)
    await system.initialize()
    return system


async def _append_archive(system: DefaultMemorySystem, summary: str) -> None:
    context = MemoryContext(session_id="test")
    archive = system.layers.archive
    assert archive is not None
    await archive.append(context, ArchiveEntry(summary=summary))


@pytest.mark.asyncio
async def test_archive_provider_no_archives(tmp_path: Path) -> None:
    """No archives → empty content, version "0"."""
    system = await _archive_system(tmp_path)
    provider = ArchiveProvider(system, MemoryContext(session_id="test"))
    content = await provider.get_or_refresh()

    assert content == ""
    assert provider.last_version == "0"


@pytest.mark.asyncio
async def test_archive_provider_with_archives(tmp_path: Path) -> None:
    """With archives → content includes summary XML, version = latest archive id."""
    system = await _archive_system(tmp_path)
    await _append_archive(system, "First conversation summary.")
    await _append_archive(system, "Second conversation summary.")
    provider = ArchiveProvider(system, MemoryContext(session_id="test"))
    content = await provider.get_or_refresh()

    assert provider.last_version not in (None, "0")
    assert "older_topics" in content
    assert "First conversation summary." in content
    assert "Second conversation summary." in content


@pytest.mark.asyncio
async def test_archive_provider_detects_new_archive(tmp_path: Path) -> None:
    """Writing a new archive changes version and refreshes content."""
    system = await _archive_system(tmp_path)
    await _append_archive(system, "Initial summary.")
    provider = ArchiveProvider(system, MemoryContext(session_id="test"))
    content1 = await provider.get_or_refresh()
    first_version = provider.last_version
    assert first_version not in (None, "0")
    assert "Initial summary." in content1

    await _append_archive(system, "Newer conversation.")

    content2 = await provider.get_or_refresh()
    assert provider.last_version != first_version
    assert "Newer conversation." in content2
    assert "Initial summary." in content2


@pytest.mark.asyncio
async def test_archive_provider_respects_inject_count(tmp_path: Path) -> None:
    """Only the last N archives are included (inject_count limit)."""
    system = await _archive_system(tmp_path)
    for i in range(1, 6):
        await _append_archive(system, f"Archive {i} content.")
    provider = ArchiveProvider(
        system,
        MemoryContext(session_id="test"),
        ArchiveInjectionConfig(count=2),
    )
    content = await provider.get_or_refresh()

    assert "Archive 5 content." in content
    assert "Archive 4 content." in content
    assert "Archive 3 content." not in content


@pytest.mark.asyncio
async def test_archive_provider_truncates_long_content(tmp_path: Path) -> None:
    """Long context.md content gets truncated at inject_max_chars."""
    long_text = "A" * 2000
    system = await _archive_system(tmp_path)
    await _append_archive(system, long_text)
    provider = ArchiveProvider(
        system,
        MemoryContext(session_id="test"),
        ArchiveInjectionConfig(max_chars=100),
    )
    content = await provider.get_or_refresh()

    assert "..." in content
    assert "AAAA" in content
    assert content.count("A") < 2000


@pytest.mark.asyncio
async def test_archive_provider_skips_empty_context(tmp_path: Path) -> None:
    """Archives with empty/missing context.md are skipped."""
    system = await _archive_system(tmp_path)
    await _append_archive(system, "Valid content.")
    await _append_archive(system, "")
    provider = ArchiveProvider(system, MemoryContext(session_id="test"))
    content = await provider.get_or_refresh()

    assert provider.last_version not in (None, "0")
    assert "Valid content." in content


# ---------------------------------------------------------------------------
# PrunedProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pruned_provider_no_content(tmp_path: Path) -> None:
    """No pruned content → version "0", empty content."""
    manager = PrunedManager(tmp_path / "pruned")

    provider = PrunedProvider(manager, session_id="test-session")
    content = await provider.get_or_refresh()

    assert content == ""
    assert provider.last_version == "0"


@pytest.mark.asyncio
async def test_pruned_provider_with_content(tmp_path: Path) -> None:
    """With pruned content → version increments, XML content generated."""
    manager = PrunedManager(tmp_path / "pruned")
    session_id = "test-session"

    now = datetime.now(tz=UTC)
    messages = [
        {"role": "user", "content": "Hello", "created_at": now},
        {"role": "assistant", "content": "Hi there", "created_at": now},
    ]

    await manager.write_pruned(messages, "Test topic", now, session_id=session_id)

    provider = PrunedProvider(manager, session_id=session_id)
    content = await provider.get_or_refresh()

    assert provider.last_version == "1"
    assert content != ""
    assert "older_topics" in content or "Previous Conversation Transcripts" in content


@pytest.mark.asyncio
async def test_pruned_provider_version_increments(tmp_path: Path) -> None:
    """Writing more pruned content increments the version."""
    manager = PrunedManager(tmp_path / "pruned")
    session_id = "test-session"

    now = datetime.now(tz=UTC)

    # First write
    msgs1 = [{"role": "user", "content": "First", "created_at": now}]
    await manager.write_pruned(msgs1, "Topic 1", now, session_id=session_id)

    provider = PrunedProvider(manager, session_id=session_id)
    await provider.get_or_refresh()
    assert provider.last_version == "1"

    # Second write
    msgs2 = [{"role": "user", "content": "Second", "created_at": now}]
    await manager.write_pruned(msgs2, "Topic 2", now, session_id=session_id)

    content = await provider.get_or_refresh()
    assert provider.last_version == "2"
    assert content != ""


@pytest.mark.asyncio
async def test_pruned_provider_different_sessions_isolated(tmp_path: Path) -> None:
    """Different session_ids get independent pruned content."""
    manager = PrunedManager(tmp_path / "pruned")

    now = datetime.now(tz=UTC)
    msgs = [{"role": "user", "content": "Hello", "created_at": now}]

    await manager.write_pruned(msgs, "Session A topic", now, session_id="session-a")
    await manager.write_pruned(msgs, "Session B topic", now, session_id="session-b")

    provider_a = PrunedProvider(manager, session_id="session-a")
    content_a = await provider_a.get_or_refresh()

    provider_b = PrunedProvider(manager, session_id="session-b")
    content_b = await provider_b.get_or_refresh()

    # Both should have content but from different sessions
    assert content_a != ""
    assert content_b != ""
    assert provider_a.last_version == "1"
    assert provider_b.last_version == "1"
    # The content references different directories
    assert "session-a" in content_a or "Session A topic" in content_a
    assert "session-b" in content_b or "Session B topic" in content_b


@pytest.mark.asyncio
async def test_pruned_provider_detects_new_content(tmp_path: Path) -> None:
    """Provider refreshes when new pruned content is written."""
    manager = PrunedManager(tmp_path / "pruned")
    session_id = "test-session"

    now = datetime.now(tz=UTC)

    provider = PrunedProvider(manager, session_id=session_id)

    # No content initially
    content1 = await provider.get_or_refresh()
    assert content1 == ""
    assert provider.last_version == "0"

    # Write content
    msgs = [{"role": "user", "content": "New message", "created_at": now}]
    await manager.write_pruned(msgs, "New topic", now, session_id=session_id)

    # Provider should detect the change
    content2 = await provider.get_or_refresh()
    assert content2 != ""
    assert provider.last_version == "1"
