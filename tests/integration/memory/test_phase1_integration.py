"""Integration test for Phase 1: DreamEngine dual trigger + knowledge templates."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from framework.ioc.configs.memory import (
    DreamEngineConfig,
    LongTermConfig,
    MemoryConfig,
)
from framework.ioc.factories.memory import _build_memory_layer_config
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager


@pytest.mark.asyncio
async def test_phase1_config_to_template_initialization(tmp_path):
    """End-to-end: config -> factory -> template initialization."""
    # Create template files
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "SOUL.md").write_text("# Test Soul", encoding="utf-8")
    (templates_dir / "USER.md").write_text("# Test User", encoding="utf-8")
    (templates_dir / "MEMORY.md").write_text("# Test Memory", encoding="utf-8")

    # Create config
    cfg = MemoryConfig(
        long_term=LongTermConfig(
            enabled=True,
            default_templates_dir=str(templates_dir),
        ),
        dream_engine=DreamEngineConfig(
            enabled=True,
            interval=600,
            min_archive_count=5,
            max_archive_count=30,
            max_batch_size=20,
        ),
    )

    # Build layer config
    layer_config = _build_memory_layer_config(cfg)

    # Verify config
    assert layer_config.knowledge is not None
    assert layer_config.knowledge.default_templates_dir == str(templates_dir)

    # Verify DreamEngineConfig values
    assert cfg.dream_engine.min_archive_count == 5
    assert cfg.dream_engine.max_archive_count == 30
    assert cfg.dream_engine.max_batch_size == 20

    # Create mock storage
    storage = AsyncMock()
    storage.get = AsyncMock(return_value=None)
    storage.set = AsyncMock()

    storage_factory = AsyncMock(return_value=storage)

    # Create knowledge manager
    manager = ScopedKnowledgeMemoryManager(
        storage_factory=storage_factory,
        config=layer_config.knowledge,
    )

    # Initialize
    context = MemoryContext(session_id="test", user_id="user1")
    await manager.ensure_defaults(context)

    # Verify templates were loaded
    assert storage.set.call_count == 3
    calls = {call.args[0]: call.args[1] for call in storage.set.call_args_list}
    assert calls["SOUL.md"] == "# Test Soul"
    assert calls["USER.md"] == "# Test User"
    assert calls["MEMORY.md"] == "# Test Memory"
