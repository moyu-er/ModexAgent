"""Unit tests for todo-related runtime types."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.runtime.todo import TodoItem, TodoStatus


def test_todo_item_frozen():
    """TodoItem is frozen — mutating a field raises ValidationError."""
    item = TodoItem(content="x", status=TodoStatus.PENDING)
    with pytest.raises(ValidationError):
        item.content = "y"  # type: ignore[misc]


def test_todo_status_values() -> None:
    assert TodoStatus.PENDING == "pending"
    assert TodoStatus.IN_PROGRESS == "in_progress"
    assert TodoStatus.COMPLETED == "completed"
    assert TodoStatus.CANCELLED == "cancelled"


def test_todo_status_from_value() -> None:
    assert TodoStatus("in_progress") is TodoStatus.IN_PROGRESS
