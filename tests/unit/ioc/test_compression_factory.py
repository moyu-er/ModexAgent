"""Tests for framework.ioc.factories.compression."""

from unittest.mock import MagicMock

from framework.ioc.configs.memory import MemoryConfig, ShortTermConfig
from framework.ioc.factories.compression import (
    create_compression_coordinator,
    create_peer_compression_coordinator,
)


class TestCreateCompressionCoordinator:
    def test_disabled_when_auto_compression_false(self) -> None:
        cfg = MemoryConfig(short_term=ShortTermConfig(auto_llm_compression=False))
        llm = MagicMock()
        assert create_compression_coordinator(cfg, llm) is None

    def test_enabled_with_defaults(self) -> None:
        cfg = MemoryConfig()
        llm = MagicMock()
        coord = create_compression_coordinator(cfg, llm)
        assert coord is not None
        assert coord._max_messages == 100


class TestCreatePeerCompressionCoordinator:
    def test_none_cfg_returns_none(self) -> None:
        assert create_peer_compression_coordinator(None) is None

    def test_peer_defaults(self) -> None:
        cfg = MemoryConfig()
        coord = create_peer_compression_coordinator(cfg)
        assert coord is not None
        assert coord._max_messages == 100
