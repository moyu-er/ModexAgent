"""Integration test for Phase 1: DreamEngine dual trigger + knowledge templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.scope import MemoryContext
from modex_agent.ioc.configs.memory import (
    DreamEngineConfig,
    LongTermConfig,
    MemoryConfig,
)
from modex_agent.ioc.factories.memory import _build_memory_layer_config
from modex_agent.memory.layers import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry


@pytest.mark.asyncio
async def test_phase1_config_to_template_initialization(tmp_path: Path) -> None:
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
            max_consume_per_run=20,
        ),
    )

    # Build layer config
    layer_config = _build_memory_layer_config(cfg)

    # Verify config
    assert layer_config.core is not None
    assert layer_config.core.default_templates_dir == str(templates_dir)

    # Verify DreamEngineConfig values
    assert cfg.dream_engine is not None
    assert cfg.dream_engine.max_consume_per_run == 20

    registry = DefaultMemoryStoreRegistry(tmp_path / "memory")
    layers = MemoryLayerFactory.build(registry=registry, config=layer_config)
    assert layers.core is not None

    # Initialize
    context = MemoryContext(session_id="test", user_id="user1")
    await layers.core.ensure_defaults(context)

    storage_path = await layers.core.get_storage_path(context)
    assert storage_path is not None
    assert {
        file_name: (storage_path / file_name).read_text(encoding="utf-8")
        for file_name in ("SOUL.md", "USER.md", "MEMORY.md")
    } == {
        "SOUL.md": "# Test Soul",
        "USER.md": "# Test User",
        "MEMORY.md": "# Test Memory",
    }
