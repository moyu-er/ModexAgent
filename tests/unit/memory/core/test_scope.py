"""Tests for memory scope abstractions."""

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.scope import (
    AgentScope,
    ChannelScope,
    ChatScope,
    CompositeScope,
    GlobalScope,
    MemoryContext,
    SessionScope,
    TenantScope,
    UserScope,
)


class TestMemoryContext:
    def test_with_defaults_returns_new_with_fills(self):
        sid = SessionInfo(session_id="s1", agent_name="unknown")
        ctx = MemoryContext(session_id=sid)
        filled = ctx.with_defaults(user_id="u_default", tenant_id="t_default")
        assert str(ctx.session_id) == "s1"
        assert ctx.user_id is None  # original unchanged
        assert filled.session_id is ctx.session_id
        assert filled.user_id == "u_default"
        assert filled.tenant_id == "t_default"

    def test_with_defaults_preserves_existing(self):
        sid = SessionInfo(session_id="s1", agent_name="unknown")
        ctx = MemoryContext(session_id=sid, user_id="u1")
        filled = ctx.with_defaults(user_id="u_default")
        assert filled.user_id == "u1"  # existing value preserved


class TestSessionScope:
    def test_get_scope_key(self):
        scope = SessionScope()
        sid = SessionInfo(session_id="sess_123", agent_name="unknown")
        assert scope.get_scope_key(MemoryContext(session_id=sid)) == "sess_123"

    def test_default_when_missing(self):
        scope = SessionScope()
        assert scope.get_scope_key(MemoryContext()) == "default"


class TestUserScope:
    def test_get_scope_key(self):
        scope = UserScope()
        assert scope.get_scope_key(MemoryContext(user_id="user_123")) == "user_123"


class TestTenantScope:
    def test_get_scope_key(self):
        scope = TenantScope()
        assert scope.get_scope_key(MemoryContext(tenant_id="tenant_123")) == "tenant_123"


class TestAgentScope:
    def test_get_scope_key(self):
        scope = AgentScope()
        assert scope.get_scope_key(MemoryContext(agent_id="agent_123")) == "agent_123"


class TestChannelScope:
    def test_get_scope_key(self):
        scope = ChannelScope()
        assert scope.get_scope_key(MemoryContext(channel="qq")) == "qq"

    def test_default_when_missing(self):
        scope = ChannelScope()
        assert scope.get_scope_key(MemoryContext()) == "default"


class TestChatScope:
    def test_get_scope_key(self):
        scope = ChatScope()
        assert scope.get_scope_key(MemoryContext(chat_id="group_123")) == "group_123"

    def test_default_when_missing(self):
        scope = ChatScope()
        assert scope.get_scope_key(MemoryContext()) == "default"


class TestGlobalScope:
    def test_always_global(self):
        scope = GlobalScope()
        # Returns empty string for clean path (no subdirectory)
        assert scope.get_scope_key(MemoryContext()) == ""
        assert scope.get_scope_key(MemoryContext(session_id="s1", user_id="u1")) == ""

    def test_global_scope_name_unchanged(self):
        scope = GlobalScope()
        assert scope.name == "global"


class TestCompositeScope:
    def test_two_scopes(self):
        scope = CompositeScope(UserScope(), SessionScope())
        sid = SessionInfo(session_id="s1", agent_name="unknown")
        ctx = MemoryContext(user_id="u1", session_id=sid)
        assert scope.get_scope_key(ctx) == "u1:s1"

    def test_three_scopes(self):
        scope = CompositeScope(TenantScope(), UserScope(), SessionScope())
        sid = SessionInfo(session_id="s1", agent_name="unknown")
        ctx = MemoryContext(tenant_id="t1", user_id="u1", session_id=sid)
        assert scope.get_scope_key(ctx) == "t1:u1:s1"

    def test_name(self):
        scope = CompositeScope(UserScope(), SessionScope())
        assert scope.name == "user:session"
