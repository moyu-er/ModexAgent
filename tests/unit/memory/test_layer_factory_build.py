"""Tests for MemoryLayerFactory.build() — the unified build path."""

from __future__ import annotations

import pytest

from framework.memory.core.layers import MemoryLayerSet
from framework.memory.core.scope import SessionScope, UserScope
from framework.memory.layers.config import (
    ArchiveMemoryConfig,
    KnowledgeMemoryConfig,
    MemoryLayerConfigSet,
    SessionMemoryConfig,
    UserRetentionBufferConfig,
)
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry import InMemoryStoreRegistry


class TestBuildFullConfig:
    """Build with all layers enabled."""

    def test_build_full_config(self) -> None:
        registry = InMemoryStoreRegistry()
        config = MemoryLayerConfigSet(
            session=SessionMemoryConfig(max_messages=100),
            archive=ArchiveMemoryConfig(),
            knowledge=KnowledgeMemoryConfig(),
            user_retention=UserRetentionBufferConfig(enabled=True),
        )
        layers = MemoryLayerFactory.build(registry=registry, config=config)

        assert isinstance(layers, MemoryLayerSet)
        assert layers.session is not None
        assert layers.archive is not None
        assert layers.knowledge is not None
        assert layers.user_retention is not None


class TestBuildSessionOnly:
    """Build with archive=None, knowledge=None."""

    def test_build_session_only(self) -> None:
        registry = InMemoryStoreRegistry()
        config = MemoryLayerConfigSet(
            session=SessionMemoryConfig(max_messages=50),
            archive=None,
            knowledge=None,
            user_retention=UserRetentionBufferConfig(enabled=True),
        )
        layers = MemoryLayerFactory.build(registry=registry, config=config)

        assert isinstance(layers, MemoryLayerSet)
        assert layers.session is not None
        assert layers.archive is None
        assert layers.knowledge is None
        assert layers.user_retention is not None


class TestBuildSubagentSessionIsolated:
    """Archive with SessionScope, no knowledge."""

    def test_build_subagent_session_isolated(self) -> None:
        registry = InMemoryStoreRegistry()
        config = MemoryLayerConfigSet(
            session=SessionMemoryConfig(max_messages=50),
            archive=ArchiveMemoryConfig(scope=SessionScope()),
            knowledge=None,
            user_retention=UserRetentionBufferConfig(enabled=True),
        )
        layers = MemoryLayerFactory.build(registry=registry, config=config)

        assert isinstance(layers, MemoryLayerSet)
        assert layers.session is not None
        assert layers.archive is not None
        assert layers.knowledge is None
        assert layers.user_retention is not None


class TestBuildDisabledUserRetention:
    """enabled=False → user_retention None."""

    def test_build_disabled_user_retention_is_none(self) -> None:
        registry = InMemoryStoreRegistry()
        config = MemoryLayerConfigSet(
            session=SessionMemoryConfig(),
            archive=ArchiveMemoryConfig(),
            knowledge=KnowledgeMemoryConfig(),
            user_retention=UserRetentionBufferConfig(enabled=False),
        )
        layers = MemoryLayerFactory.build(registry=registry, config=config)

        assert isinstance(layers, MemoryLayerSet)
        assert layers.user_retention is None
