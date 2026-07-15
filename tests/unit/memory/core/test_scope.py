"""Tests for memory scope abstractions."""

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
    scope_path_key,
)


class TestMemoryContext:
    def test_with_defaults_returns_new_with_fills(self):
        ctx = MemoryContext(session_id="s1")
        filled = ctx.with_defaults(user_id="u_default", tenant_id="t_default")
        assert ctx.session_id == "s1"
        assert ctx.user_id is None  # original unchanged
        assert filled.session_id == "s1"
        assert filled.user_id == "u_default"
        assert filled.tenant_id == "t_default"

    def test_with_defaults_preserves_existing(self):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        filled = ctx.with_defaults(user_id="u_default")
        assert filled.user_id == "u1"  # existing value preserved

    def test_session_id_must_be_str_not_session_info(self):
        """session_id is a session-id string; a SessionInfo object is rejected."""
        import pytest
        from pydantic import ValidationError

        from modex_agent.core.session_id import SessionInfo

        with pytest.raises(ValidationError):
            MemoryContext(session_id=SessionInfo(session_id="s1", agent_name="unknown"))


class TestSessionScope:
    def test_path_key(self):
        scope = SessionScope()
        assert scope_path_key(scope, MemoryContext(session_id="sess_123")) == "sess_123"

    def test_default_when_missing(self):
        scope = SessionScope()
        assert scope_path_key(scope, MemoryContext()) == "default"


class TestUserScope:
    def test_path_key(self):
        scope = UserScope()
        assert scope_path_key(scope, MemoryContext(user_id="user_123")) == "user_123"


class TestTenantScope:
    def test_path_key(self):
        scope = TenantScope()
        assert scope_path_key(scope, MemoryContext(tenant_id="tenant_123")) == "tenant_123"


class TestAgentScope:
    def test_path_key(self):
        scope = AgentScope()
        context = MemoryContext(agent_id="agent_123", agent_role="subagent")
        assert scope_path_key(scope, context) == "agent_123:subagent"


class TestChannelScope:
    def test_path_key(self):
        scope = ChannelScope()
        assert scope_path_key(scope, MemoryContext(channel="qq")) == "qq"

    def test_default_when_missing(self):
        scope = ChannelScope()
        assert scope_path_key(scope, MemoryContext()) == "default"


class TestChatScope:
    def test_path_key(self):
        scope = ChatScope()
        assert scope_path_key(scope, MemoryContext(chat_id="group_123")) == "group_123"

    def test_default_when_missing(self):
        scope = ChatScope()
        assert scope_path_key(scope, MemoryContext()) == "default"


class TestGlobalScope:
    def test_always_global(self):
        scope = GlobalScope()
        # Returns empty string for clean path (no subdirectory)
        assert scope_path_key(scope, MemoryContext()) == ""
        assert scope_path_key(scope, MemoryContext(session_id="s1", user_id="u1")) == ""

    def test_global_scope_name_unchanged(self):
        scope = GlobalScope()
        assert scope.name == "global"


class TestCompositeScope:
    def test_two_scopes(self):
        scope = CompositeScope(UserScope(), SessionScope())
        ctx = MemoryContext(user_id="u1", session_id="s1")
        assert scope_path_key(scope, ctx) == "u1:s1"

    def test_three_scopes(self):
        scope = CompositeScope(TenantScope(), UserScope(), SessionScope())
        ctx = MemoryContext(tenant_id="t1", user_id="u1", session_id="s1")
        assert scope_path_key(scope, ctx) == "t1:u1:s1"

    def test_name(self):
        scope = CompositeScope(UserScope(), SessionScope())
        assert scope.name == "user:session"
