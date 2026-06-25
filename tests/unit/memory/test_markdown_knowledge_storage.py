"""Tests for MarkdownKnowledgeStorage — stores knowledge as actual .md files."""
from __future__ import annotations

import pytest
from pathlib import Path

from modex_agent.core.scope import MemoryLayerName


def _make_storage(tmp_path: Path):
    """Create a MarkdownKnowledgeStorage instance."""
    from modex_agent.memory.stores.markdown_knowledge import MarkdownKnowledgeStorage

    storage = MarkdownKnowledgeStorage(
        tmp_path,
        layer=MemoryLayerName.KNOWLEDGE,
    )
    return storage


class TestMarkdownKnowledgeStorage:
    """Verify knowledge files are stored as actual .md files on disk."""

    @pytest.mark.asyncio
    async def test_set_creates_actual_md_file(self, tmp_path):
        """Setting SOUL.md should create a real SOUL.md file on disk."""
        storage = _make_storage(tmp_path)
        await storage.initialize()

        await storage.set("SOUL.md", "# Hello\nI am an assistant.")

        md_file = tmp_path / "SOUL.md"
        assert md_file.exists()
        assert md_file.read_text(encoding="utf-8") == "# Hello\nI am an assistant."

    @pytest.mark.asyncio
    async def test_get_reads_actual_md_file(self, tmp_path):
        """Getting SOUL.md should read from the real file, not kv.json."""
        storage = _make_storage(tmp_path)
        await storage.initialize()

        # Write a real file
        (tmp_path / "SOUL.md").write_text("# Test Soul", encoding="utf-8")

        result = await storage.get("SOUL.md")
        assert result == "# Test Soul"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_file(self, tmp_path):
        """Getting a non-existent file should return None."""
        storage = _make_storage(tmp_path)
        await storage.initialize()

        result = await storage.get("NONEXISTENT.md")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_removes_md_file(self, tmp_path):
        """Deleting a key should remove the actual .md file."""
        storage = _make_storage(tmp_path)
        await storage.initialize()

        (tmp_path / "MEMORY.md").write_text("# Memory", encoding="utf-8")
        assert (tmp_path / "MEMORY.md").exists()

        result = await storage.delete("MEMORY.md")
        assert result is True
        assert not (tmp_path / "MEMORY.md").exists()

    @pytest.mark.asyncio
    async def test_list_keys_returns_md_files(self, tmp_path):
        """list_keys should return .md filenames from the directory."""
        storage = _make_storage(tmp_path)
        await storage.initialize()

        (tmp_path / "SOUL.md").write_text("# Soul", encoding="utf-8")
        (tmp_path / "USER.md").write_text("# User", encoding="utf-8")
        (tmp_path / "MEMORY.md").write_text("# Memory", encoding="utf-8")

        keys = await storage.list_keys()
        assert set(keys) >= {"SOUL.md", "USER.md", "MEMORY.md"}

    @pytest.mark.asyncio
    async def test_overwrite_existing_md_file(self, tmp_path):
        """Setting an existing file should overwrite it."""
        storage = _make_storage(tmp_path)
        await storage.initialize()

        (tmp_path / "SOUL.md").write_text("old content", encoding="utf-8")
        await storage.set("SOUL.md", "new content")

        assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "new content"

    @pytest.mark.asyncio
    async def test_non_md_keys_fall_through_to_kv_json(self, tmp_path):
        """Non-.md keys (metadata) should still use kv.json."""
        storage = _make_storage(tmp_path)
        await storage.initialize()

        await storage.set(".last_activity", 12345)

        # Should NOT create a .last_activity file
        assert not (tmp_path / ".last_activity").exists()
        # Should be in kv.json
        result = await storage.get(".last_activity")
        assert result == 12345

    @pytest.mark.asyncio
    async def test_template_copy_creates_real_files(self, tmp_path):
        """Template initialization should create actual .md files on disk."""
        storage = _make_storage(tmp_path)
        await storage.initialize()

        # Simulate template initialization
        await storage.set("SOUL.md", "# Default Soul\nBe helpful.")
        await storage.set("USER.md", "# User Profile\n(unknown)")
        await storage.set("MEMORY.md", "# Memory\n(empty)")

        # All three files should exist as real .md files
        assert (tmp_path / "SOUL.md").exists()
        assert (tmp_path / "USER.md").exists()
        assert (tmp_path / "MEMORY.md").exists()
        assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "# Default Soul\nBe helpful."
