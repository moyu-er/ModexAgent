"""Unit tests for todo-related runtime types."""
from __future__ import annotations


def test_todo_probe_key_is_transient_underscore_prefix():
    """The probe state machine key is "_"-prefixed so it follows the
    transient (never-persisted) custom-key convention."""
    from modex_agent.runtime.enums import TurnCustomKey

    assert TurnCustomKey.TODO_PROBE.value == "_todo_probe"
