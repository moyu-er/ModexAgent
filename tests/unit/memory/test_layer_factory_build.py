"""Tests for MemoryLayerFactory.build() — the unified build path."""

from __future__ import annotations

from pathlib import Path

from modex_agent.core.scope import SessionScope
from modex_agent.memory.core.layers import MemoryLayerSet
from modex_agent.memory.layers.config import (
    ArchiveMemoryConfig,
    CoreMemoryConfig,
    MemoryLayerConfigSet,
    SessionMemoryConfig,
)
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry


class TestBuildFullConfig:
    """Build with all layers enabled."""

    def test_build_full_config(self, tmp_path: Path) -> None:
        registry = DefaultMemoryStoreRegistry(tmp_path)
        config = MemoryLayerConfigSet(
            session=SessionMemoryConfig(),
            archive=ArchiveMemoryConfig(),
            core=CoreMemoryConfig(),
        )
        layers = MemoryLayerFactory.build(registry=registry, config=config)

        assert isinstance(layers, MemoryLayerSet)
        assert layers.session is not None
        assert layers.archive is not None
        assert layers.core is not None


class TestBuildSessionOnly:
    """Build with archive=None, core=None."""

    def test_build_session_only(self, tmp_path: Path) -> None:
        registry = DefaultMemoryStoreRegistry(tmp_path)
        config = MemoryLayerConfigSet(
            session=SessionMemoryConfig(),
            archive=None,
            core=None,
        )
        layers = MemoryLayerFactory.build(registry=registry, config=config)

        assert isinstance(layers, MemoryLayerSet)
        assert layers.session is not None
        assert layers.archive is None
        assert layers.core is None


class TestBuildSubagentSessionIsolated:
    """Archive with SessionScope, no knowledge."""

    def test_build_subagent_session_isolated(self, tmp_path: Path) -> None:
        registry = DefaultMemoryStoreRegistry(tmp_path)
        config = MemoryLayerConfigSet(
            session=SessionMemoryConfig(),
            archive=ArchiveMemoryConfig(scope=SessionScope()),
            core=None,
        )
        layers = MemoryLayerFactory.build(registry=registry, config=config)

        assert isinstance(layers, MemoryLayerSet)
        assert layers.session is not None
        assert layers.archive is not None
        assert layers.core is None
