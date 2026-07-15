"""Tests for the runtime context subsystem.

Covers:
- InMemoryRuntimeContext: generic state + tool tracking
- InMemoryRuntimeContextStore: scope isolation
- RuntimeContextManager: session-scoped context lifecycle
"""

from __future__ import annotations

import pytest

from modex_agent.core.runtime_context import (
    InMemoryRuntimeContext,
    InMemoryRuntimeContextStore,
    RuntimeContextManager,
    ToolCallRecord,
)
from modex_agent.core.scope import MemoryContext, SessionScope, UserScope
from modex_agent.core.session_id import SessionInfo


def _session(session_id: str) -> SessionInfo:
    """Build a SessionInfo for the manager API (which reads session.session_id).

    Constructed directly (not via from_str) so the bare-id test keys carry no
    '.' separator without tripping from_str's UserWarning.
    """
    return SessionInfo(session_id=session_id, agent_name="test")


class TestInMemoryRuntimeContext:
    """Verify generic state + tool-call tracking."""

    async def test_clear_empties_all_state(self):
        ctx = InMemoryRuntimeContext()
        await ctx.set("foo", "bar")
        await ctx.record_tool_call("search", {"q": "x"}, "result")
        assert await ctx.has("foo")
        assert len(await ctx.get_tool_calls()) == 1

        await ctx.clear()

        assert not await ctx.has("foo")
        assert await ctx.get_tool_calls() == []

    async def test_generic_set_get_has(self):
        ctx = InMemoryRuntimeContext()
        assert not await ctx.has("key")
        assert await ctx.get("key", "default") == "default"

        await ctx.set("key", 42)
        assert await ctx.has("key")
        assert await ctx.get("key") == 42

    async def test_record_tool_call_appends(self):
        ctx = InMemoryRuntimeContext()
        await ctx.record_tool_call("tool_a", {"x": 1}, "r1")
        await ctx.record_tool_call("tool_b", {"y": 2}, "r2")

        calls = await ctx.get_tool_calls()
        assert len(calls) == 2
        assert calls[0].tool_name == "tool_a"
        assert calls[1].tool_name == "tool_b"
        assert calls[0].arguments == {"x": 1}
        assert calls[0].result == "r1"
        assert isinstance(calls[0].timestamp, float)

    async def test_has_called(self):
        ctx = InMemoryRuntimeContext()
        assert not await ctx.has_called("send_to_agent")

        await ctx.record_tool_call("send_to_agent", {"target_agent": "main"}, "ok")
        assert await ctx.has_called("send_to_agent")
        assert not await ctx.has_called("other_tool")

    async def test_tool_calls_are_immutable(self):
        ctx = InMemoryRuntimeContext()
        await ctx.record_tool_call("t", {"a": 1}, "r")
        calls = await ctx.get_tool_calls()
        assert isinstance(calls[0], ToolCallRecord)
        # frozen dataclass
        with pytest.raises(AttributeError):
            calls[0].tool_name = "x"  # type: ignore[misc]


class TestInMemoryRuntimeContextStore:
    """Verify per-scope isolation."""

    async def test_get_or_create_returns_same_instance(self):
        store = InMemoryRuntimeContextStore()
        ctx1 = await store.get_or_create("scope_a")
        ctx2 = await store.get_or_create("scope_a")
        assert ctx1 is ctx2

    async def test_different_scopes_get_isolated_contexts(self):
        store = InMemoryRuntimeContextStore()
        ctx_a = await store.get_or_create("scope_a")
        ctx_b = await store.get_or_create("scope_b")
        assert ctx_a is not ctx_b

        await ctx_a.set("key", "a")
        assert await ctx_b.get("key") is None

    async def test_clear_clears_context(self):
        store = InMemoryRuntimeContextStore()
        ctx = await store.get_or_create("scope_a")
        await ctx.set("key", "value")
        await store.clear("scope_a")
        assert not await ctx.has("key")

    async def test_clear_unknown_scope_noop(self):
        store = InMemoryRuntimeContextStore()
        await store.clear("nonexistent")  # should not raise


class TestRuntimeContextManager:
    """Verify manager wires scope + store correctly."""

    async def test_default_session_scope_isolation(self):
        mgr = RuntimeContextManager()
        ctx_a = await mgr.get_context(_session("session_1"))
        ctx_b = await mgr.get_context(_session("session_2"))
        assert ctx_a is not ctx_b

        await ctx_a.set("k", "v")
        assert not await ctx_b.has("k")

    async def test_user_scope_aggregates_sessions(self):
        mgr = RuntimeContextManager(scope=UserScope())
        # Same user_id → same scope key even with different session_id
        ctx1 = await mgr.get_context(_session("s1"), {"user_id": "user_1"})
        ctx2 = await mgr.get_context(_session("s2"), {"user_id": "user_1"})
        assert ctx1 is ctx2

        ctx3 = await mgr.get_context(_session("s3"), {"user_id": "user_2"})
        assert ctx3 is not ctx1

    async def test_clear_context(self):
        mgr = RuntimeContextManager()
        ctx = await mgr.get_context(_session("session_x"))
        await ctx.record_tool_call("t", {}, "r")
        assert len(await ctx.get_tool_calls()) == 1

        await mgr.clear_context(_session("session_x"))
        assert await ctx.get_tool_calls() == []

    async def test_manager_reuses_store(self):
        store = InMemoryRuntimeContextStore()
        mgr = RuntimeContextManager(store=store)
        ctx = await mgr.get_context(_session("s1"))
        # Same store + SessionScope → scope_key is the canonical form of the
        # session RecordScope, so a direct store lookup by that key returns
        # the same instance.
        scope_key = SessionScope().extract(MemoryContext(session_id="s1")).canonical()
        ctx2 = await store.get_or_create(scope_key)
        assert ctx is ctx2
