from __future__ import annotations

import inspect

from framework.memory.core.system import MemorySystem


def test_memory_system_abc_excludes_prompt_assembly() -> None:
    expected = {
        "initialize",
        "close",
        "create_message_history",
        "add_messages",
        "get_history",
        "search",
        "clear",
    }

    assert inspect.isabstract(MemorySystem)
    assert expected.issubset(MemorySystem.__abstractmethods__)
    assert not hasattr(MemorySystem, "build_context")
