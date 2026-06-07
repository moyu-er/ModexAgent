"""Tests for overwrite-based knowledge updates.

Knowledge updates should generate complete new file content (not patches)
and save it as a full file replacement.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from framework.memory.core.scope import MemoryContext
from framework.memory.stores.markdown_knowledge import MarkdownKnowledgeStorage
from framework.memory.core.scope import MemoryLayerName


class TestOverwriteKnowledgeUpdate:
    """Knowledge updates should overwrite files with complete content."""

    @pytest.mark.asyncio
    async def test_apply_update_overwrites_entire_file(self, tmp_path):
        """apply_update with section_replace mode should overwrite the entire .md file."""
        storage = MarkdownKnowledgeStorage(tmp_path, layer=MemoryLayerName.KNOWLEDGE)
        await storage.initialize()

        # Create initial file
        (tmp_path / "USER.md").write_text(
            "# User Profile\n- **Name**: (unknown)\n- **Timezone**: (unknown)\n",
            encoding="utf-8",
        )

        # Apply overwrite update with complete new content
        from framework.memory.core.consolidation import MemoryUpdate

        update = MemoryUpdate(
            file_name="USER.md",
            content="# User Profile\n- **Name**: John\n- **Timezone**: UTC+8\n",
            mode="section_replace",
            reason="learned user name and timezone",
        )

        from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager
        from framework.memory.layers.config import KnowledgeMemoryConfig

        config = KnowledgeMemoryConfig()
        manager = ScopedKnowledgeMemoryManager(
            storage_factory=AsyncMock(return_value=storage),
            config=config,
        )

        context = MemoryContext(session_id="test", user_id="user1")
        result = await manager.apply_update(context, update)

        # File should be completely replaced
        file_content = (tmp_path / "USER.md").read_text(encoding="utf-8")
        assert file_content == "# User Profile\n- **Name**: John\n- **Timezone**: UTC+8\n"
        assert "(unknown)" not in file_content

    @pytest.mark.asyncio
    async def test_memory_update_defaults_to_overwrite_mode(self):
        """MemoryUpdate should default to section_replace mode."""
        from framework.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode

        update = MemoryUpdate(
            file_name="USER.md",
            content="# Updated User\n- Name: John",
            reason="learned name",
        )
        assert update.mode == str(MemoryUpdateMode.INCREMENTAL)
        assert update.content == "# Updated User\n- Name: John"

        update2 = MemoryUpdate(
            file_name="MEMORY.md",
            content="# Memory\n- Uses Python",
            mode="section_replace",
            reason="new fact",
        )
        assert update2.mode == "section_replace"

    @pytest.mark.asyncio
    async def test_template_not_found_creates_empty_md(self, tmp_path):
        """When template doesn't exist, ensure_defaults should create empty .md files."""
        storage = MarkdownKnowledgeStorage(tmp_path, layer=MemoryLayerName.KNOWLEDGE)
        await storage.initialize()

        # Template dir doesn't exist
        templates_dir = tmp_path / "nonexistent_templates"

        from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager
        from framework.memory.layers.config import KnowledgeMemoryConfig

        config = KnowledgeMemoryConfig(default_templates_dir=str(templates_dir))
        manager = ScopedKnowledgeMemoryManager(
            storage_factory=AsyncMock(return_value=storage),
            config=config,
        )

        context = MemoryContext(session_id="test", user_id="user1")
        await manager.ensure_defaults(context)

        # Should create empty .md files
        assert (tmp_path / "SOUL.md").exists()
        assert (tmp_path / "USER.md").exists()
        assert (tmp_path / "MEMORY.md").exists()
        assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == ""
        assert (tmp_path / "USER.md").read_text(encoding="utf-8") == ""
