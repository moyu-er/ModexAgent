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
    from framework.core.provider import LLMProvider

    class MockProvider(LLMProvider):
        async def chat(self, messages, **kwargs):
            from framework.core.types import LLMResponse
            return LLMResponse(content="ok")

        def get_default_model(self):
            return "test-model"

    return MockProvider()


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

    def test_archive_agent_none_when_archive_disabled(self, tmp_path: Path) -> None:
        """When archive layer is disabled, archive_agent should be None."""
        cfg = MemoryConfig()
        system = create_memory(cfg, _make_provider(), tmp_path)
        assert system._archive_agent is None

    def test_archive_agent_created_when_archive_enabled(self, tmp_path: Path) -> None:
        """When archive layer is enabled, archive_agent should be created."""
        from framework.ioc.configs.memory import ArchiveConfig

        cfg = MemoryConfig(archive=ArchiveConfig(enabled=True))
        system = create_memory(cfg, _make_provider(), tmp_path)
        assert system._archive_agent is not None

    def test_knowledge_consolidator_created_when_knowledge_enabled(
        self, tmp_path: Path,
    ) -> None:
        """When knowledge layer is enabled, knowledge_consolidator should be created."""
        from framework.ioc.configs.memory import (
            ArchiveConfig,
            KnowledgeConfig,
        )

        cfg = MemoryConfig(
            archive=ArchiveConfig(enabled=True),
            knowledge=KnowledgeConfig(enabled=True),
        )
        system = create_memory(cfg, _make_provider(), tmp_path)
        assert system._knowledge_consolidator is not None


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
