from framework.core.types import TodoStatus


def test_todo_status_values() -> None:
    assert TodoStatus.PENDING == "pending"
    assert TodoStatus.IN_PROGRESS == "in_progress"
    assert TodoStatus.COMPLETED == "completed"
    assert TodoStatus.CANCELLED == "cancelled"


def test_todo_status_from_value() -> None:
    assert TodoStatus("in_progress") is TodoStatus.IN_PROGRESS
