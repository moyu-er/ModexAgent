"""Runtime context for agent execution.

Provides a generic, scope-isolated runtime state container that lives for a
single agent turn.  Hooks and tools can read/write arbitrary state through a
session-scoped RuntimeContext, managed by RuntimeContextManager.

Layering:
- runtime/context.py     → generic ABCs + in-memory defaults
- agents/react/agent.py  → ReActAgent clears/records tool calls each turn
- runtime/hooks.py       → RuntimeContextHook records communication-tool calls

Moved from core/runtime_context.py (plan §15 B2); the single-production-adapter
RuntimeContextStore hierarchy was folded into RuntimeContextManager.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.session_id import SessionInfo

from .models import JsonValue

#: Internal key used by :meth:`InMemoryRuntimeContext.record_tool_call`.
_TOOL_CALLS_KEY = "_tool_calls"


class ToolCallRecord(BaseModel):
    """Immutable record of a single tool invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    arguments: dict[str, JsonValue]
    result: JsonValue
    timestamp: float = Field(default_factory=time.time)


class RuntimeContext(ABC):
    """Abstract runtime context for a single agent turn.

    Concrete subclasses are **extensible state containers**; they must
    support generic key-value storage (``set / get / has``) so that any
    hook or tool can stash arbitrary data.

    The default :class:`InMemoryRuntimeContext` additionally tracks tool
    calls; future subclasses may track file operations, network requests,
    etc., on top of the same generic storage.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def clear(self) -> None:
        """Reset all state. Called by the agent at the start of each turn."""

    # ------------------------------------------------------------------
    # Generic key-value state
    # ------------------------------------------------------------------

    @abstractmethod
    async def set(self, key: str, value: Any) -> None:
        """Store *value* under *key*."""

    @abstractmethod
    async def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, or *default* if absent."""

    @abstractmethod
    async def has(self, key: str) -> bool:
        """Return whether *key* exists."""

    # ------------------------------------------------------------------
    # Tool-call tracking (standard capability for agent frameworks)
    # ------------------------------------------------------------------

    @abstractmethod
    async def record_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, JsonValue],
        result: JsonValue,
    ) -> None:
        """Append a tool call record."""

    @abstractmethod
    async def get_tool_calls(self) -> list[ToolCallRecord]:
        """Return all recorded tool calls in order."""

    @abstractmethod
    async def has_called(self, tool_name: str) -> bool:
        """Check whether a tool with the given name was invoked."""


class InMemoryRuntimeContext(RuntimeContext):
    """Default in-memory runtime context.

    Uses a plain ``dict`` for generic storage and a list under the internal
    key ``_tool_calls`` for tool invocation records.

    Safe for single-asyncio-task use (the ReAct loop runs tools sequentially
    within one turn).
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    # -- lifecycle --

    async def clear(self) -> None:
        self._data.clear()

    # -- generic state --

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    async def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def has(self, key: str) -> bool:
        return key in self._data

    # -- tool tracking --

    async def record_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, JsonValue],
        result: JsonValue,
    ) -> None:
        calls: list[ToolCallRecord] = self._data.setdefault(_TOOL_CALLS_KEY, [])
        calls.append(
            ToolCallRecord(
                tool_name=tool_name,
                arguments=dict(arguments),
                result=result,
            )
        )

    async def get_tool_calls(self) -> list[ToolCallRecord]:
        return list(self._data.get(_TOOL_CALLS_KEY, []))

    async def has_called(self, tool_name: str) -> bool:
        calls: list[ToolCallRecord] = self._data.get(_TOOL_CALLS_KEY, [])
        return any(c.tool_name == tool_name for c in calls)


class RuntimeContextManager:
    """Central manager that owns per-session RuntimeContext instances.

    Each ``session_id`` gets its own isolated context. The former separate
    store abstraction (exactly one in-memory adapter, zero production
    ``store=`` callers) is folded into this manager, which owns the mapping
    directly.
    """

    def __init__(self) -> None:
        self._contexts: dict[str, InMemoryRuntimeContext] = {}

    async def get_context(self, session: SessionInfo) -> RuntimeContext:
        """Return the RuntimeContext for *session* (creating if needed)."""
        session_id = session.session_id
        if session_id not in self._contexts:
            self._contexts[session_id] = InMemoryRuntimeContext()
        return self._contexts[session_id]

    async def clear_context(self, session: SessionInfo) -> None:
        """Clear the RuntimeContext for *session*."""
        ctx = self._contexts.get(session.session_id)
        if ctx is not None:
            await ctx.clear()
