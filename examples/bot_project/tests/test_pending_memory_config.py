from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.service.core import BotService

from framework.memory.layers.config import PendingPrunedInputMemoryConfig


def _service() -> BotService:
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


def test_pending_config_defaults_enabled_for_main_memory() -> None:
    service = _service()
    config = service._build_memory_layer_config({"short_term": {"max_messages": 10}})
    assert isinstance(config.pending, PendingPrunedInputMemoryConfig)
    assert config.pending.enabled is True


def test_pending_config_can_be_disabled_for_main_memory() -> None:
    service = _service()
    config = service._build_memory_layer_config({
        "short_term": {"max_messages": 10},
        "pending_pruned_inputs": {"enabled": False},
    })
    assert config.pending is None


def test_subagent_compression_uses_generic_pending_config_defaults() -> None:
    service = _service()
    memory_config = service._session_only_memory_config({"short_term": {"max_messages": 10}})
    assert memory_config.pending is not None
    assert memory_config.pending.enabled is True
