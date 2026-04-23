"""Runtime context for agent execution.

Provides a generic, scope-isolated runtime state container that lives for a
single agent turn.  Hooks and tools can read/write arbitrary state through a
session-scoped RuntimeContext, managed by RuntimeContextManager.

Layering:
- core/runtime_context.py  → generic ABCs + in-memory defaults
- agents/react/agent.py    → ReActAgent clears/records tool calls each turn
- multi_agent/hooks.py     → PeerAutoSendHook reads communication-tool calls
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from framework.memory.core.scope import MemoryContext, MemoryScope, SessionScope

#: Internal key used by :meth:`InMemoryRuntimeContext.record_tool_call`.
_TOOL_CALLS_KEY = "_tool_calls"


@dataclass(frozen=True)
class ToolCallRecord:
    """Immutable record of a single tool invocation."""

    tool_name: str
    arguments: dict[str, Any]
    result: Any
    timestamp: float = field(default_factory=time.time)


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
        arguments: dict[str, Any],
        result: Any,
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
        arguments: dict[str, Any],
        result: Any,
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


class RuntimeContextStore(ABC):
    """Abstract storage backend for per-scope RuntimeContext instances."""

    @abstractmethod
    async def get_or_create(self, scope_key: str) -> RuntimeContext:
        """Return the RuntimeContext for *scope_key*, creating one if absent."""

    @abstractmethod
    async def clear(self, scope_key: str) -> None:
        """Clear the RuntimeContext for *scope_key* if it exists."""


class InMemoryRuntimeContextStore(RuntimeContextStore):
    """In-memory store backed by a plain dict."""

    def __init__(self) -> None:
        self._contexts: dict[str, InMemoryRuntimeContext] = {}

    async def get_or_create(self, scope_key: str) -> RuntimeContext:
        if scope_key not in self._contexts:
            self._contexts[scope_key] = InMemoryRuntimeContext()
        return self._contexts[scope_key]

    async def clear(self, scope_key: str) -> None:
        ctx = self._contexts.get(scope_key)
        if ctx is not None:
            await ctx.clear()


class RuntimeContextManager:
    """Central manager that owns a store + scope and hands out isolated
    :class:`RuntimeContext` instances per session.

    The *scope* (a :class:`MemoryScope`) determines how sessions are grouped.
    By default :class:`SessionScope` is used, so each ``session_id`` gets its
    own isolated context.
    """

    def __init__(
        self,
        store: RuntimeContextStore | None = None,
        scope: MemoryScope | None = None,
    ) -> None:
        self._store = store or InMemoryRuntimeContextStore()
        self._scope = scope or SessionScope()

    async def get_context(
        self,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeContext:
        """Return the RuntimeContext for *session_id* (creating if needed)."""
        scope_key = self._resolve_scope_key(session_id, metadata)
        return await self._store.get_or_create(scope_key)

    async def clear_context(
        self,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Clear the RuntimeContext for *session_id*."""
        scope_key = self._resolve_scope_key(session_id, metadata)
        await self._store.clear(scope_key)

    def _resolve_scope_key(
        self, session_id: str, metadata: dict[str, Any] | None
    ) -> str:
        meta = metadata or {}
        mem_ctx = MemoryContext(
            session_id=session_id,
            user_id=meta.get("user_id"),
            tenant_id=meta.get("tenant_id"),
            agent_id=meta.get("agent_id"),
            channel=meta.get("channel"),
            chat_id=meta.get("chat_id"),
            sender_agent=meta.get("sender_agent"),
            receiver_agent=meta.get("receiver_agent"),
        )
        return self._scope.get_scope_key(mem_ctx)
