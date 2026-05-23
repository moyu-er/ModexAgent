"""Tests for framework.ioc.factories.compression."""

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.factories.compression import create_subagent_compression_coordinator


class TestCreateSubagentCompressionCoordinator:
    def test_none_cfg_returns_none(self) -> None:
        assert create_subagent_compression_coordinator(None) is None

    def test_subagent_defaults(self) -> None:
        cfg = MemoryConfig()
        coord = create_subagent_compression_coordinator(cfg)
        assert coord is not None
        assert coord._max_messages == 100
