"""Regression tests for q2: compression coordinator config pass-through.

Ensures BotService._build_compression_coordinator forwards short_term
thresholds from bot_config.yml to DefaultMemoryCompressionCoordinator.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.service.core import BotService


@pytest.fixture
def mock_service() -> BotService:
    """Return a BotService with mocked adapters so _build_compression_coordinator can run."""
    input_adapter = MagicMock()
    input_adapter.name = "mock_input"
    output_adapter = MagicMock()
    output_adapter.name = "mock_output"
    emitter_factory = MagicMock()

    service = BotService(
        config_dir=Path("."),
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        emitter_factory=emitter_factory,
    )
    service.provider = MagicMock()
    return service


def test_build_compression_coordinator_passes_short_term_config(mock_service: BotService) -> None:
    """Coordinator must receive max_messages/max_tokens/keep_ratio from config, not defaults."""
    main_memory_config: dict[str, Any] = {
        "short_term": {
            "max_messages": 150,
            "max_tokens": 120000,
            "keep_ratio_for_messages": 0.35,
            "keep_ratio_for_token": 0.25,
            "auto_llm_compression": True,
        },
        "compaction": {
            "policy": "conservative",
            "boundary": "tool_chain",
        },
    }

    coordinator = mock_service._build_compression_coordinator(main_memory_config)

    assert coordinator is not None
    trigger = coordinator._trigger
    assert trigger._max_messages == 150
    assert trigger._max_tokens == 120000
    assert coordinator._keep_ratio_for_messages == 0.35
    assert coordinator._keep_ratio_for_token == 0.25


def test_build_compression_coordinator_uses_defaults_when_config_missing(mock_service: BotService) -> None:
    """When short_term config is absent, coordinator should use sensible defaults."""
    main_memory_config: dict[str, Any] = {
        "short_term": {
            "auto_llm_compression": True,
        },
        "compaction": {
            "policy": "conservative",
        },
    }

    coordinator = mock_service._build_compression_coordinator(main_memory_config)

    assert coordinator is not None
    trigger = coordinator._trigger
    assert trigger._max_messages == 100
    assert trigger._max_tokens == 8000
    assert coordinator._keep_ratio_for_messages == 0.5
    assert coordinator._keep_ratio_for_token == 0.5


def test_build_compression_coordinator_returns_none_when_compression_disabled(mock_service: BotService) -> None:
    """When both auto_llm_compression and auto_compact are disabled, return None."""
    main_memory_config: dict[str, Any] = {
        "short_term": {
            "auto_llm_compression": False,
        },
        "auto_compact": {
            "enabled": False,
        },
    }

    coordinator = mock_service._build_compression_coordinator(main_memory_config)

    assert coordinator is None
