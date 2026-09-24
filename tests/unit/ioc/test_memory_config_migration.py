"""Tests for config schema migration."""
from __future__ import annotations

import logging

from modex_agent.ioc.configs.memory import (
    ArchiveConfig,
    CoreMemoryConfig,
    MemoryConfig,
    SessionConfig,
)


def test_session_config_exists():
    cfg = SessionConfig()
    assert cfg.max_context_tokens == 200000
    assert cfg.max_token_ratio == 0.85
    assert not hasattr(cfg, "keep_ratio")


def test_archive_config_exists():
    cfg = ArchiveConfig()
    assert cfg.enabled is False
    assert cfg.max_entries == 1000
    assert cfg.retained_consumed_pairs == 3


def test_knowledge_config_exists():
    cfg = CoreMemoryConfig()
    assert cfg.enabled is False
    assert cfg.default_templates_dir is None


def test_memory_config_has_new_fields():
    cfg = MemoryConfig()
    assert hasattr(cfg, "session")
    assert hasattr(cfg, "archive")
    assert hasattr(cfg, "core")


def test_memory_config_accepts_old_keys():
    """Old short_term/long_term should map to new session/archive/knowledge.

    Only max_context_tokens survives the token-based redesign.
    """
    data = {
        "short_term": {"max_context_tokens": 50000},
        "long_term": {"enabled": True, "default_templates_dir": "templates/knowledge"},
    }
    cfg = MemoryConfig(**data)
    assert cfg.session.max_context_tokens == 50000
    assert cfg.session.max_token_ratio == 0.85
    assert not hasattr(cfg.session, "keep_ratio")
    assert cfg.archive.enabled is True
    assert cfg.core.enabled is True
    assert cfg.core.default_templates_dir == "templates/knowledge"


def test_session_config_ignores_removed_keep_ratio():
    """A legacy config still carrying a tail keep ratio parses cleanly.

    The tail keep budget is the engine's absolute formula
    clamp(usable × 0.25, 2000, 15000) (PRD §4.4.7) — the removed field has
    no successor knob. Old user configs that set it are silently ignored
    (plain BaseModel: extra keys are ignored, not rejected).
    """
    cfg = MemoryConfig(**{"session": {"max_context_tokens": 50000, "keep_ratio": 0.9}})
    assert cfg.session.max_context_tokens == 50000
    assert not hasattr(cfg.session, "keep_ratio")


def test_memory_config_accepts_new_keys():
    data = {
        "session": {"max_context_tokens": 75000},
        "archive": {"enabled": True, "max_entries": 500},
        "core": {"enabled": True, "default_templates_dir": "templates"},
    }
    cfg = MemoryConfig(**data)
    assert cfg.session.max_context_tokens == 75000
    assert cfg.archive.enabled is True
    assert cfg.archive.max_entries == 500
    assert cfg.core.enabled is True
    assert cfg.core.default_templates_dir == "templates"


def test_memory_config_warns_on_old_keys(caplog):
    with caplog.at_level(logging.WARNING):
        cfg = MemoryConfig(**{"short_term": {"max_context_tokens": 50000}})
    assert "deprecated" in caplog.text.lower()
    assert cfg.session.max_context_tokens == 50000
