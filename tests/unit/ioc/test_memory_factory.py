"""Tests for framework.ioc.factories.memory -- create_memory behavior."""
from __future__ import annotations

from pathlib import Path

import pytest

from framework.ioc.configs.memory import (
    MemoryConfig,
    ShortTermConfig,
)
from framework.ioc.factories.memory import create_memory


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
