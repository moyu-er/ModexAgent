"""Tests for template directory config and polymorphic template initialization."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from framework.ioc.configs.memory import LongTermConfig
from framework.memory.layers.config import KnowledgeMemoryConfig
from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager
from framework.memory.core.scope import MemoryContext


# ---------------------------------------------------------------------------
# LongTermConfig template dir tests
# ---------------------------------------------------------------------------


def test_long_term_config_has_template_dir():
    """LongTermConfig should have a default_templates_dir field."""
    assert hasattr(LongTermConfig, "model_fields")
    assert "default_templates_dir" in LongTermConfig.model_fields


def test_long_term_config_default_template_dir():
    """default_templates_dir should default to None."""
    config = LongTermConfig()
    assert config.default_templates_dir is None


def test_long_term_config_custom_template_dir():
    """default_templates_dir should accept a custom path string."""
    config = LongTermConfig(default_templates_dir="/tmp/templates")
    assert config.default_templates_dir == "/tmp/templates"


# ---------------------------------------------------------------------------
# KnowledgeManager template loading tests
# ---------------------------------------------------------------------------


def _make_manager(
    templates_dir: str | None = None,
) -> ScopedKnowledgeMemoryManager:
    """Create a ScopedKnowledgeMemoryManager with a mock storage factory."""
    storage = AsyncMock()
    storage.get = AsyncMock(return_value=None)
    storage.set = AsyncMock()
    factory = AsyncMock(return_value=storage)
    config = KnowledgeMemoryConfig(default_templates_dir=templates_dir)
    return ScopedKnowledgeMemoryManager(factory, config=config)


async def test_knowledge_manager_loads_from_templates(tmp_path: Path):
    """When template files exist, ensure_defaults loads their content."""
    # Create template files
    (tmp_path / "SOUL.md").write_text("template soul", encoding="utf-8")
    (tmp_path / "USER.md").write_text("template user", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("template memory", encoding="utf-8")

    manager = _make_manager(templates_dir=str(tmp_path))
    ctx = MemoryContext(session_id="s1", user_id="u1")

    await manager.ensure_defaults(ctx)

    storage = await manager._storage_factory(ctx)
    calls = {c.args[0]: c.args[1] for c in storage.set.call_args_list}
    assert calls["SOUL.md"] == "template soul"
    assert calls["USER.md"] == "template user"
    assert calls["MEMORY.md"] == "template memory"


async def test_knowledge_manager_skips_existing_files(tmp_path: Path):
    """Non-empty existing content should NOT be overwritten by templates."""
    (tmp_path / "SOUL.md").write_text("template soul", encoding="utf-8")

    storage = AsyncMock()
    # SOUL.md exists with content; USER.md and MEMORY.md are missing
    storage.get = AsyncMock(
        side_effect=lambda key: {
            "SOUL.md": "existing soul",
            "USER.md": None,
            "MEMORY.md": "",
        }.get(key)
    )
    storage.set = AsyncMock()
    factory = AsyncMock(return_value=storage)
    config = KnowledgeMemoryConfig(default_templates_dir=str(tmp_path))
    manager = ScopedKnowledgeMemoryManager(factory, config=config)
    ctx = MemoryContext(session_id="s1", user_id="u1")

    await manager.ensure_defaults(ctx)

    calls = {c.args[0]: c.args[1] for c in storage.set.call_args_list}
    assert "SOUL.md" not in calls  # non-empty existing → skipped
    assert "USER.md" in calls      # missing → loaded from template
    assert "MEMORY.md" in calls    # empty string → treated as empty, loaded from template


async def test_knowledge_manager_handles_missing_template(tmp_path: Path):
    """When template file doesn't exist, content falls back to defaults dict."""
    # Only create SOUL.md template, not USER.md or MEMORY.md
    (tmp_path / "SOUL.md").write_text("template soul", encoding="utf-8")

    manager = _make_manager(templates_dir=str(tmp_path))
    ctx = MemoryContext(session_id="s1", user_id="u1")

    await manager.ensure_defaults(
        ctx,
        defaults={"user": "default user", "memory": "default memory"},
    )

    storage = await manager._storage_factory(ctx)
    calls = {c.args[0]: c.args[1] for c in storage.set.call_args_list}
    assert calls["SOUL.md"] == "template soul"
    assert calls["USER.md"] == "default user"
    assert calls["MEMORY.md"] == "default memory"


async def test_knowledge_manager_works_without_templates():
    """When default_templates_dir is None, ensure_defaults still works."""
    manager = _make_manager(templates_dir=None)
    ctx = MemoryContext(session_id="s1", user_id="u1")

    await manager.ensure_defaults(ctx)

    storage = await manager._storage_factory(ctx)
    calls = {c.args[0]: c.args[1] for c in storage.set.call_args_list}
    # All files created with empty string
    assert calls["SOUL.md"] == ""
    assert calls["USER.md"] == ""
    assert calls["MEMORY.md"] == ""
