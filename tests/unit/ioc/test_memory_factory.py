"""Tests for framework.ioc.factories.memory — create_memory behavior."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from framework.ioc.configs.memory import (
    LongTermConfig,
    MemoryConfig,
    ShortTermConfig,
)
from framework.ioc.factories.memory import create_memory


def _make_provider() -> AsyncMock:
    """Create a mock LLMProvider with get_default_model()."""
    provider = AsyncMock()
    provider.get_default_model.return_value = "test-model"
    return provider


class TestCreateMemoryAutoCompact:
    def test_auto_compact_false_creates_compression_coordinator(self, tmp_path: Path) -> None:
        """When auto_compact=False, compression coordinator must still exist.

        Regression: the old auto_llm_compression flag used to skip the entire
        coordinator when set to False, meaning session memory would grow past
        max_messages without any pruning.
        """
        cfg = MemoryConfig(
            short_term=ShortTermConfig(
                max_messages=50,
                auto_compact=False,
            ),
        )
        system = create_memory(cfg, _make_provider(), tmp_path)
        coordinator = system.compression_coordinator
        assert coordinator is not None, (
            "auto_compact=False must still create a compression coordinator "
            "so that max_messages is enforced. Only the LLM archive generation is skipped."
        )

    def test_auto_compact_false_uses_max_messages(self, tmp_path: Path) -> None:
        """Compression trigger must use the configured max_messages."""
        cfg = MemoryConfig(
            short_term=ShortTermConfig(
                max_messages=200,
                auto_compact=False,
            ),
        )
        system = create_memory(cfg, _make_provider(), tmp_path)
        coordinator = system.compression_coordinator
        assert coordinator._trigger.max_messages == 200

    def test_auto_compact_false_no_llm_archive_generation(self, tmp_path: Path) -> None:
        """When auto_compact=False, LLM archive generation must be skipped."""
        cfg = MemoryConfig(
            short_term=ShortTermConfig(
                max_messages=50,
                auto_compact=False,
            ),
        )
        system = create_memory(cfg, _make_provider(), tmp_path)
        coordinator = system.compression_coordinator
        assert coordinator._archive_generation is None

    def test_auto_compact_true_creates_llm_archive_generation(self, tmp_path: Path) -> None:
        """When auto_compact=True (default), LLM archive generation is created."""
        cfg = MemoryConfig(
            short_term=ShortTermConfig(
                max_messages=50,
                auto_compact=True,
            ),
            long_term=LongTermConfig(enabled=True),
        )
        system = create_memory(cfg, _make_provider(), tmp_path)
        coordinator = system.compression_coordinator
        assert coordinator is not None
        assert coordinator._archive_generation is not None

    def test_auto_compact_true_default(self, tmp_path: Path) -> None:
        """Default ShortTermConfig has auto_compact=True."""
        cfg = MemoryConfig()
        system = create_memory(cfg, _make_provider(), tmp_path)
        coordinator = system.compression_coordinator
        assert coordinator is not None
        assert coordinator._archive_generation is not None

    def test_lifecycle_created_when_auto_compact_false(self, tmp_path: Path) -> None:
        """Lifecycle policy must exist even when auto_compact=False."""
        cfg = MemoryConfig(
            short_term=ShortTermConfig(auto_compact=False),
        )
        system = create_memory(cfg, _make_provider(), tmp_path)
        assert system._lifecycle is not None
