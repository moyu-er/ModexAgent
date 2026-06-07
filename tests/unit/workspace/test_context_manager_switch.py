"""Verify that context_manager.memory_system (no underscore) is updated on switch.

The real MemorySystemContextManager stores the memory at ``self.memory_system``
(system.py:127), NOT ``self._memory_system``. Our callbacks must use the
correct attribute name.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class SpyContextManager:
    """Mimics MemorySystemContextManager's attribute layout.

    ``memory_system`` (no leading underscore) is the real name — see
    framework/memory/system.py:127.
    """

    def __init__(self, memory_system: object) -> None:
        self.memory_system = memory_system


class SpyMemory:
    def __init__(self, label: str = "") -> None:
        self.label = label
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def initialize(self) -> None:
        pass


@pytest.mark.asyncio
async def test_wrong_underscore_attr_does_not_update():
    """Setting _memory_system (with underscore) is a silent no-op —
    the real attribute ``memory_system`` is unchanged."""
    old = SpyMemory("old")
    new = SpyMemory("new")
    ctx = SpyContextManager(old)

    # Bug: _memory_system doesn't exist, creates a new instance attribute
    # that no one reads — the real .memory_system stays pointing to old.
    ctx._memory_system = new  # type: ignore[attr-defined]

    assert ctx.memory_system is old  # ← still old! _memory_system is ignored
    assert getattr(ctx, "_memory_system", None) is new  # ← goes to a dead attribute


@pytest.mark.asyncio
async def test_correct_attr_updates_as_expected():
    """Setting .memory_system (no underscore) correctly updates the reference."""
    old = SpyMemory("old")
    new = SpyMemory("new")
    ctx = SpyContextManager(old)

    ctx.memory_system = new

    assert ctx.memory_system is new
    assert ctx.memory_system is not old
