"""InjectionFilterStrategy: pluggable message filtering for LLM context assembly.

Replaces scattered ``filter_tool_messages`` boolean logic with a typed strategy
so callers can swap filtering behaviour without changing policy code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from framework.memory.compression.tool_chain import _is_tool_call, _is_tool_result
from framework.memory.core.message import ChatMessage


class InjectionFilterStrategy(ABC):
    """Filter messages before they are injected into the LLM context."""

    @abstractmethod
    def filter(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """Return the subset of *messages* that should be visible to the LLM."""
        raise NotImplementedError


class ToolMessageFilterStrategy(InjectionFilterStrategy):
    """Drop tool-call and tool-result messages to reduce token waste."""

    def filter(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        return [
            msg
            for msg in messages
            if not (_is_tool_call(msg) or _is_tool_result(msg))
        ]


class NoopFilterStrategy(InjectionFilterStrategy):
    """Pass every message through unchanged."""

    def filter(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        return list(messages)
