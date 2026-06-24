"""Tests for MarkdownKnowledgeStorage changelog compatibility.

The changelog tracks updates to knowledge files (append_log/read_logs/save_logs).
These methods are inherited from DefaultScopedStorage, which writes to changelog.jsonl.
MarkdownKnowledgeStorage must NOT interfere with non-.md key operations.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from modex_agent.memory.core.scope import MemoryLayerName


def _make_storage(tmp_path: Path):
    """Create a MarkdownKnowledgeStorage instance."""
    from modex_agent.memory.stores.markdown_knowledge import MarkdownKnowledgeStorage

    storage = MarkdownKnowledgeStorage(
        tmp_path,
        layer=MemoryLayerName.KNOWLEDGE,
    )
    return storage


@pytest.mark.asyncio
async def test_append_log_writes_to_changelog_jsonl(tmp_path):
    """Changelog entries should go to changelog.jsonl, not individual .md files."""
    storage = _make_storage(tmp_path)
    await storage.initialize()

    await storage.append_log({
        "file": "SOUL.md",
        "mode": "append",
        "reason": "test update",
    })

    changelog = tmp_path / "changelog.jsonl"
    assert changelog.exists(), "changelog.jsonl should exist after append_log"
    content = changelog.read_text(encoding="utf-8")
    assert "test update" in content
    # Should NOT create a separate .md file for the log entry
    assert not (tmp_path / "test update.md").exists()


@pytest.mark.asyncio
async def test_read_logs_from_changelog_jsonl(tmp_path):
    """Changelog should be readable via read_logs."""
    storage = _make_storage(tmp_path)
    await storage.initialize()

    await storage.append_log({"file": "SOUL.md", "mode": "append", "reason": "r1"})
    await storage.append_log({"file": "USER.md", "mode": "replace", "reason": "r2"})

    logs = await storage.read_logs(since_cursor=0)
    assert len(logs) == 2
    reasons = {entry.get("reason") for entry in logs}
    assert "r1" in reasons
    assert "r2" in reasons


@pytest.mark.asyncio
async def test_save_logs_overwrites_changelog_jsonl(tmp_path):
    """save_logs should atomically replace the changelog.

    read_logs filters on cursor > since_cursor (default 0), so entries
    written via save_logs must include cursor fields to be visible.
    """
    storage = _make_storage(tmp_path)
    await storage.initialize()

    await storage.append_log({"file": "SOUL.md", "mode": "append", "reason": "old"})
    # save_logs writes entries: include cursor so read_logs(since_cursor=0) returns them
    await storage.save_logs([
        {"file": "USER.md", "mode": "append", "reason": "new", "cursor": 5}
    ])

    logs = await storage.read_logs(since_cursor=0)
    reasons = [entry.get("reason") for entry in logs]
    assert "new" in reasons
    assert "old" not in reasons


@pytest.mark.asyncio
async def test_md_set_and_changelog_independent(tmp_path):
    """Setting .md files should not interfere with changelog operations."""
    storage = _make_storage(tmp_path)
    await storage.initialize()

    # Set a knowledge file
    await storage.set("SOUL.md", "# Test Soul")
    # Append a changelog entry
    await storage.append_log({"file": "SOUL.md", "mode": "set", "reason": "init"})

    # Verify .md file exists
    assert (tmp_path / "SOUL.md").exists()
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "# Test Soul"

    # Verify changelog exists
    assert (tmp_path / "changelog.jsonl").exists()

    # list_keys should return the .md file (not the changelog key)
    keys = await storage.list_keys()
    assert "SOUL.md" in keys
    # changelog.jsonl is NOT returned by list_keys (it's not a .md file and not in kv)
    assert "changelog.jsonl" not in keys


@pytest.mark.asyncio
async def test_list_keys_includes_both_md_and_kv_keys(tmp_path):
    """list_keys should return both .md filenames and non-.md kv.json keys."""
    storage = _make_storage(tmp_path)
    await storage.initialize()

    # Write .md files
    (tmp_path / "SOUL.md").write_text("# Soul", encoding="utf-8")
    # Write non-.md keys through kv.json
    await storage.set(".archive_state", {"cursor": 5})
    await storage.set(".last_activity", 12345)

    keys = await storage.list_keys()
    assert "SOUL.md" in keys
    # Non-.md keys should also be returned (they come from kv.json parent)
    assert ".archive_state" in keys
    assert ".last_activity" in keys
