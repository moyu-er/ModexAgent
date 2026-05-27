"""Tests for framework.ioc.factories.memory -- create_memory behavior."""
from __future__ import annotations

from pathlib import Path

import pytest

from framework.ioc.configs.memory import (
    MemoryConfig,
    ShortTermConfig,
)
from framework.ioc.factories.memory import create_memory, _build_memory_layer_config


def _make_provider():
    """Create a mock LLMProvider with get_default_model()."""
    from unittest.mock import AsyncMock
    provider = AsyncMock()
    provider.get_default_model.return_value = "test-model"
    return provider


class TestCreateMemoryCleanupConfig:
    def test_cleanup_config_uses_max_messages(self, tmp_path: Path) -> None:
        """cleanup_config must use the configured max_messages."""
        cfg = MemoryConfig(
            short_term=ShortTermConfig(
                max_messages=200,
            ),
        )
        system = create_memory(cfg, _make_provider(), tmp_path)
        assert system._cleanup_config["max_messages"] == 200

    def test_cleanup_config_present_by_default(self, tmp_path: Path) -> None:
        """Default ShortTermConfig must have cleanup_config with max_messages."""
        cfg = MemoryConfig()
        system = create_memory(cfg, _make_provider(), tmp_path)
        assert "max_messages" in system._cleanup_config
        assert system._cleanup_config["max_messages"] == 100

    def test_no_compression_coordinator_attribute(self, tmp_path: Path) -> None:
        """The old compression_coordinator attribute must not exist."""
        cfg = MemoryConfig()
        system = create_memory(cfg, _make_provider(), tmp_path)
        assert not hasattr(system, "compression_coordinator")

    def test_archive_strategy_none_when_no_llm(self, tmp_path: Path) -> None:
        """When llm_provider is None, archive_strategy should be None."""
        cfg = MemoryConfig()
        system = create_memory(cfg, None, tmp_path)
        assert system._archive_strategy is None

    def test_archive_strategy_created_with_llm(self, tmp_path: Path) -> None:
        """When llm_provider is provided, archive_strategy should be created."""
        cfg = MemoryConfig()
        system = create_memory(cfg, _make_provider(), tmp_path)
        assert system._archive_strategy is not None


class TestBuildMemoryLayerConfigNewSchema:
    def test_build_memory_layer_config_uses_new_config(self) -> None:
        """Should use new config fields (session, archive, knowledge)."""
        from framework.ioc.configs.memory import (
            MemoryConfig,
            SessionConfig,
            ArchiveConfig,
            KnowledgeConfig,
        )

        cfg = MemoryConfig(
            session=SessionConfig(max_messages=250),
            archive=ArchiveConfig(enabled=True, max_entries=800),
            knowledge=KnowledgeConfig(
                enabled=True,
                default_templates_dir="templates/knowledge",
            ),
        )

        layer_config = _build_memory_layer_config(cfg)

        assert layer_config.session.max_messages == 250
        assert layer_config.archive is not None
        assert layer_config.knowledge is not None
        assert layer_config.knowledge.default_templates_dir == "templates/knowledge"

    def test_build_memory_layer_config_handles_disabled_archive(self) -> None:
        """archive.enabled=False should result in no archive layer."""
        from framework.ioc.configs.memory import MemoryConfig, ArchiveConfig

        cfg = MemoryConfig(
            archive=ArchiveConfig(enabled=False),
        )

        layer_config = _build_memory_layer_config(cfg)

        assert layer_config.archive is None

    def test_build_memory_layer_config_handles_disabled_knowledge(self) -> None:
        """knowledge.enabled=False should result in no knowledge layer."""
        from framework.ioc.configs.memory import MemoryConfig, KnowledgeConfig

        cfg = MemoryConfig(
            knowledge=KnowledgeConfig(enabled=False),
        )

        layer_config = _build_memory_layer_config(cfg)

        assert layer_config.knowledge is None
