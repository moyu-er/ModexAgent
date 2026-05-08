from __future__ import annotations

from framework.core.types import MessageRole
from framework.memory.core.layers import MemoryLayerSet, PendingPrunedInputMemoryManager
from framework.memory.core.scope import MemoryLayerName, SessionScope
from framework.memory.layers.config import PendingPrunedInputMemoryConfig


def test_pending_role_is_internal_message_role() -> None:
    assert MessageRole.PENDING == "pending"


def test_pending_layer_name_exists() -> None:
    assert MemoryLayerName.PENDING == "pending"


def test_pending_config_defaults_enabled_and_session_scoped() -> None:
    config = PendingPrunedInputMemoryConfig()
    assert config.enabled is True
    assert config.max_entries == 8
    assert config.max_chars == 12000
    assert isinstance(config.scope, SessionScope)


def test_memory_layer_set_accepts_optional_pending_manager() -> None:
    class DummyPending(PendingPrunedInputMemoryManager):
        async def append_entries(self, context, entries):
            return None

        async def get_entries(self, context):
            return []

        async def clear(self, context):
            return None

    class DummySession:
        pass

    pending = DummyPending()
    layer_set = MemoryLayerSet(session=DummySession(), pending=pending)  # type: ignore[arg-type]
    assert layer_set.pending is pending
