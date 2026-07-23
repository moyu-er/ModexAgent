"""Unit tests for todo-related runtime types."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.core.types import TodoStatus
from modex_agent.runtime.store import TodoItem


def test_todo_probe_key_is_transient_underscore_prefix():
    """The probe state machine key is "_"-prefixed so it follows the
    transient (never-persisted) custom-key convention."""
    from modex_agent.runtime.enums import TurnCustomKey

    assert TurnCustomKey.TODO_PROBE.value == "_todo_probe"


def test_todo_item_frozen():
    """TodoItem is frozen — mutating a field raises ValidationError."""
    item = TodoItem(content="x", status=TodoStatus.PENDING)
    with pytest.raises(ValidationError):
        item.content = "y"  # type: ignore[misc]
