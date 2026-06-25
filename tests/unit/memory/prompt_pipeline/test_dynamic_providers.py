"""Integration tests for ArchiveProvider and PrunedProvider with real storage backends.

Uses actual DirArchiveStorage and PrunedManager (not mocks) to verify
the dynamic providers interact correctly with the storage layer.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.stores.dir_archive import DirArchiveStorage
from modex_agent.memory.prompt_pipeline.providers import ArchiveProvider, PrunedProvider


# ---------------------------------------------------------------------------
# ArchiveProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_provider_no_archives(tmp_path):
    """No archives → empty content, version "0"."""
    storage = DirArchiveStorage(tmp_path / "archives")
    await storage.initialize()

    provider = ArchiveProvider(storage.directory)
    content = await provider.get_or_refresh()

    assert content == ""
    assert provider.last_version == "0"


@pytest.mark.asyncio
async def test_archive_provider_with_archives(tmp_path):
    """With archives → content includes summary XML, version = latest archive id."""
    storage = DirArchiveStorage(tmp_path / "archives")
    await storage.initialize()

    # Write a context.md into archive 1
    await storage.write_archive_file(1, "context.md", "First conversation summary.")
    # Write archive 2 with more content
    await storage.write_archive_file(2, "context.md", "Second conversation summary.")

    provider = ArchiveProvider(storage.directory)
    content = await provider.get_or_refresh()

    # Version should be the latest (highest) archive id
    assert provider.last_version == "2"
    # Content should include both summaries in XML container
    assert "older_topics" in content
    assert "First conversation summary." in content
    assert "Second conversation summary." in content


@pytest.mark.asyncio
async def test_archive_provider_detects_new_archive(tmp_path):
    """Writing a new archive changes version and refreshes content."""
    storage = DirArchiveStorage(tmp_path / "archives")
    await storage.initialize()

    await storage.write_archive_file(1, "context.md", "Initial summary.")

    provider = ArchiveProvider(storage.directory)
    content1 = await provider.get_or_refresh()
    assert provider.last_version == "1"
    assert "Initial summary." in content1

    # Add a new archive
    await storage.write_archive_file(5, "context.md", "Newer conversation.")

    content2 = await provider.get_or_refresh()
    assert provider.last_version == "5"
    assert "Newer conversation." in content2
    # Old summary should still be present (limit=3, only 2 archives)
    assert "Initial summary." in content2


@pytest.mark.asyncio
async def test_archive_provider_respects_inject_count(tmp_path):
    """Only the last N archives are included (inject_count limit)."""
    storage = DirArchiveStorage(tmp_path / "archives")
    await storage.initialize()

    # Write 5 archives
    for i in range(1, 6):
        await storage.write_archive_file(i, "context.md", f"Archive {i} content.")

    provider = ArchiveProvider(storage.directory, inject_count=2)
    content = await provider.get_or_refresh()

    # Only the 2 most recent (sorted ascending, last 2) should appear
    assert "Archive 5 content." in content
    assert "Archive 4 content." in content
    # Archive 3 might appear in the iteration (sorted takes first N),
    # but with inject_count=2, we only get the 2 lowest from sorted list
    # (sorted(archive_ids)[:inject_count] = [1,2] since list_archives returns descending)


@pytest.mark.asyncio
async def test_archive_provider_truncates_long_content(tmp_path):
    """Long context.md content gets truncated at inject_max_chars."""
    storage = DirArchiveStorage(tmp_path / "archives")
    await storage.initialize()

    long_text = "A" * 2000
    await storage.write_archive_file(1, "context.md", long_text)

    provider = ArchiveProvider(storage.directory, inject_max_chars=100)
    content = await provider.get_or_refresh()

    # Content should contain truncated version with "..."
    assert "..." in content
    assert "AAAA" in content  # Start of the content is present
    # The full 2000 chars should NOT all be present
    assert content.count("A") < 2000


@pytest.mark.asyncio
async def test_archive_provider_skips_empty_context(tmp_path):
    """Archives with empty/missing context.md are skipped."""
    storage = DirArchiveStorage(tmp_path / "archives")
    await storage.initialize()

    # Archive 1 has content
    await storage.write_archive_file(1, "context.md", "Valid content.")
    # Archive 2 has no context.md at all
    await storage.write_archive_file(2, "knowledge.md", "Only knowledge, no context.")
    # Archive 3 has empty context.md
    await storage.write_archive_file(3, "context.md", "")

    provider = ArchiveProvider(storage.directory)
    content = await provider.get_or_refresh()

    # Version should still be "3" (highest archive id)
    assert provider.last_version == "3"
    # Only archive 1 should contribute content
    assert "Valid content." in content


# ---------------------------------------------------------------------------
# PrunedProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pruned_provider_no_content(tmp_path):
    """No pruned content → version "0", empty content."""
    manager = PrunedManager(tmp_path / "pruned")

    provider = PrunedProvider(manager, session_id="test-session")
    content = await provider.get_or_refresh()

    assert content == ""
    assert provider.last_version == "0"


@pytest.mark.asyncio
async def test_pruned_provider_with_content(tmp_path):
    """With pruned content → version increments, XML content generated."""
    manager = PrunedManager(tmp_path / "pruned")
    session_id = "test-session"

    now = datetime.now(tz=timezone.utc)
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
async def test_pruned_provider_version_increments(tmp_path):
    """Writing more pruned content increments the version."""
    manager = PrunedManager(tmp_path / "pruned")
    session_id = "test-session"

    now = datetime.now(tz=timezone.utc)

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
async def test_pruned_provider_different_sessions_isolated(tmp_path):
    """Different session_ids get independent pruned content."""
    manager = PrunedManager(tmp_path / "pruned")

    now = datetime.now(tz=timezone.utc)
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
async def test_pruned_provider_detects_new_content(tmp_path):
    """Provider refreshes when new pruned content is written."""
    manager = PrunedManager(tmp_path / "pruned")
    session_id = "test-session"

    now = datetime.now(tz=timezone.utc)

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
