"""Tests for config schema migration."""
from __future__ import annotations

import logging

import pytest

from modex_agent.ioc.configs.memory import (
    ArchiveConfig,
    KnowledgeConfig,
    MemoryConfig,
    SessionConfig,
)


def test_session_config_exists():
    cfg = SessionConfig()
    assert cfg.max_messages == 100
    assert cfg.max_tokens == 100000


def test_archive_config_exists():
    cfg = ArchiveConfig()
    assert cfg.enabled is False
    assert cfg.max_entries == 1000
    assert cfg.retained_consumed_pairs == 3


def test_knowledge_config_exists():
    cfg = KnowledgeConfig()
    assert cfg.enabled is False
    assert cfg.default_templates_dir is None


def test_memory_config_has_new_fields():
    cfg = MemoryConfig()
    assert hasattr(cfg, "session")
    assert hasattr(cfg, "archive")
    assert hasattr(cfg, "knowledge")


def test_memory_config_accepts_old_keys():
    """Old short_term/long_term should map to new session/archive/knowledge."""
    data = {
        "short_term": {"max_messages": 200},
        "long_term": {"enabled": True, "default_templates_dir": "templates/knowledge"},
    }
    cfg = MemoryConfig(**data)
    assert cfg.session.max_messages == 200
    assert cfg.archive.enabled is True
    assert cfg.knowledge.enabled is True
    assert cfg.knowledge.default_templates_dir == "templates/knowledge"


def test_memory_config_accepts_new_keys():
    data = {
        "session": {"max_messages": 300},
        "archive": {"enabled": True, "max_entries": 500},
        "knowledge": {"enabled": True, "default_templates_dir": "templates"},
    }
    cfg = MemoryConfig(**data)
    assert cfg.session.max_messages == 300
    assert cfg.archive.enabled is True
    assert cfg.archive.max_entries == 500
    assert cfg.knowledge.enabled is True
    assert cfg.knowledge.default_templates_dir == "templates"


def test_memory_config_warns_on_old_keys(caplog):
    with caplog.at_level(logging.WARNING):
        cfg = MemoryConfig(**{"short_term": {"max_messages": 200}})
    assert "deprecated" in caplog.text.lower()
