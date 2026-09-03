"""Tests for knowledge template path resolution.

The default_templates_dir in config (e.g. 'templates/knowledge') is a
relative path. When resolved via Path(), it depends on the current working
directory. If the bot is started from outside the project directory,
templates won't be found. We need a test to verify this behavior and
potentially fix it.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.memory.layers.config import CoreMemoryConfig
from modex_agent.memory.scope import MemoryContext


@pytest.mark.asyncio
async def test_template_path_resolves_relative_to_cwd(tmp_path, monkeypatch):
    """Template path is resolved relative to CWD by default."""
    # Create templates in a subdirectory of tmp_path
    templates_dir = tmp_path / "my_project" / "templates" / "knowledge"
    templates_dir.mkdir(parents=True)
    (templates_dir / "SOUL.md").write_text("# Project Soul", encoding="utf-8")

    # Save current CWD, change to project dir
    old_cwd = os.getcwd()
    monkeypatch.chdir(tmp_path / "my_project")

    try:
        # Create a config with relative path
        config = CoreMemoryConfig(
            default_templates_dir="templates/knowledge",
        )

        # Simulate ensure_defaults logic
        from modex_agent.memory.layers.core import ScopedCoreMemoryManager

        storage = AsyncMock()
        storage.get = AsyncMock(return_value=None)
        storage.set = AsyncMock()
        bundle = MagicMock()
        bundle.kv = storage
        bundle.archive = None
        storage_factory = AsyncMock(return_value=bundle)

        manager = ScopedCoreMemoryManager(
            storage_factory=storage_factory,
            config=config,
        )
        context = MemoryContext(session_id="test", user_id="u1")
        await manager.ensure_defaults(context)

        # Template should have been found
        calls = {call.args[0]: call.args[1] for call in storage.set.call_args_list}
        assert calls.get("SOUL.md") == "# Project Soul"
    finally:
        monkeypatch.chdir(old_cwd)


@pytest.mark.asyncio
async def test_template_path_absolute_works_anywhere(tmp_path):
    """Absolute template paths should work regardless of CWD."""
    templates_dir = tmp_path / "templates" / "knowledge"
    templates_dir.mkdir(parents=True)
    (templates_dir / "USER.md").write_text("# Absolute User", encoding="utf-8")

    config = CoreMemoryConfig(
        default_templates_dir=str(templates_dir.resolve()),
    )

    storage = AsyncMock()
    storage.get = AsyncMock(return_value=None)
    storage.set = AsyncMock()
    bundle = MagicMock()
    bundle.kv = storage
    bundle.archive = None
    storage_factory = AsyncMock(return_value=bundle)

    from modex_agent.memory.layers.core import ScopedCoreMemoryManager

    manager = ScopedCoreMemoryManager(
        storage_factory=storage_factory,
        config=config,
    )
    context = MemoryContext(session_id="test", user_id="u1")
    await manager.ensure_defaults(context)

    calls = {call.args[0]: call.args[1] for call in storage.set.call_args_list}
    assert calls.get("USER.md") == "# Absolute User"
