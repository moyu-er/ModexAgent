"""Tests for ArchiveConfig and DreamEngineConfig."""
from __future__ import annotations

from modex_agent.ioc.configs.memory import ArchiveConfig, DreamEngineConfig


def test_archive_config_defaults():
    cfg = ArchiveConfig()
    assert cfg.enabled is False
    assert cfg.max_entries == 1000
    assert cfg.retained_consumed_pairs == 3
    assert cfg.max_archive_count == 10
    assert cfg.max_archive_total == 20
    assert cfg.max_archive_inject == 3


def test_dream_engine_config_defaults():
    cfg = DreamEngineConfig()
    assert cfg.enabled is False
    assert cfg.interval == 1200
    assert cfg.max_consume_per_run == 3


def test_archive_config_custom_values():
    cfg = ArchiveConfig(
        enabled=True,
        max_archive_count=5,
        max_archive_total=15,
        max_archive_inject=2,
    )
    assert cfg.max_archive_count == 5
    assert cfg.max_archive_total == 15
    assert cfg.max_archive_inject == 2


def test_dream_engine_config_custom_values():
    cfg = DreamEngineConfig(
        enabled=True,
        interval=300,
        max_consume_per_run=5,
    )
    assert cfg.max_consume_per_run == 5
