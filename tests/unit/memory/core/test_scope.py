"""Tests for memory scope abstractions."""

import pytest
from pydantic import ValidationError

from modex_agent.memory.scope import (
    AgentScope,
    ChannelScope,
    ChatScope,
    CompositeScope,
    GlobalScope,
    MemoryContext,
    MemoryLayerName,
    ScopeRecord,
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
        from modex_agent.core.session_id import SessionInfo

        with pytest.raises(ValidationError):
            MemoryContext(
                session_id=SessionInfo(  # type: ignore[arg-type]
                    session_id="s1", agent_name="unknown"
                )
            )

    def test_frozen_and_extra_forbidden(self):
        ctx = MemoryContext(session_id="s1")
        with pytest.raises(ValidationError):
            ctx.user_id = "u1"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            MemoryContext(session_id="s1", unknown="value")  # type: ignore[call-arg]


class TestScopeRecord:
    def test_model_round_trip(self):
        record = ScopeRecord(
            scope_key="s1",
            layer=MemoryLayerName.SESSION,
            context=MemoryContext(session_id="s1"),
            storage_path="/tmp/memory/session/s1",
        )

        assert ScopeRecord.model_validate(record.model_dump()) == record

    def test_frozen_and_extra_forbidden(self):
        record = ScopeRecord(
            scope_key="s1",
            layer=MemoryLayerName.SESSION,
            context=MemoryContext(session_id="s1"),
            storage_path="/tmp/memory/session/s1",
        )
        with pytest.raises(ValidationError):
            record.scope_key = "s2"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            ScopeRecord(
                scope_key="s1",
                layer=MemoryLayerName.SESSION,
                context=MemoryContext(session_id="s1"),
                storage_path="/tmp/memory/session/s1",
                unknown="value",  # type: ignore[call-arg]
            )


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
