"""Tests for InjectionResult replacing MemoryContextBundle."""
from __future__ import annotations

from modex_agent.core.message import ChatMessage
from modex_agent.memory.core.models import InjectionResult


def test_injection_result_construction():
    msgs = [ChatMessage(role="user", content="hello")]
    result = InjectionResult(system_prompt="## Knowledge\n...", messages=msgs)
    assert result.system_prompt == "## Knowledge\n..."
    assert result.messages == msgs
    assert len(result.messages) == 1


def test_injection_result_empty():
    result = InjectionResult(system_prompt="", messages=[])
    assert result.system_prompt == ""


def test_injection_result_is_dataclass():
    r1 = InjectionResult(system_prompt="a", messages=[])
    r2 = InjectionResult(system_prompt="a", messages=[])
    assert r1 == r2
