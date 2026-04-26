"""Tests for memory scope abstractions."""

from framework.memory.core.scope import (
    AgentScope,
    ChannelScope,
    ChatScope,
    CompositeScope,
    GlobalScope,
    MemoryContext,
    PeerPairScope,
    SessionScope,
    TenantScope,
    UserScope,
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


class TestSessionScope:
    def test_get_scope_key(self):
        scope = SessionScope()
        assert scope.get_scope_key(MemoryContext(session_id="sess_123")) == "sess_123"

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
        assert scope.get_scope_key(MemoryContext()) == "global"
        assert scope.get_scope_key(MemoryContext(session_id="s1", user_id="u1")) == "global"


class TestCompositeScope:
    def test_two_scopes(self):
        scope = CompositeScope(UserScope(), SessionScope())
        ctx = MemoryContext(user_id="u1", session_id="s1")
        assert scope.get_scope_key(ctx) == "u1:s1"

    def test_three_scopes(self):
        scope = CompositeScope(TenantScope(), UserScope(), SessionScope())
        ctx = MemoryContext(tenant_id="t1", user_id="u1", session_id="s1")
        assert scope.get_scope_key(ctx) == "t1:u1:s1"

    def test_name(self):
        scope = CompositeScope(UserScope(), SessionScope())
        assert scope.name == "user:session"


class TestPeerPairScope:
    def test_three_segment_session_id_passthrough(self):
        """三段式 session_id 应直接透传。"""
        scope = PeerPairScope()
        ctx = MemoryContext(session_id="conv_001:sender:receiver")
        assert scope.get_scope_key(ctx) == "conv_001:sender:receiver"

    def test_construct_from_context_fields(self):
        """从独立字段构造 scope key。"""
        scope = PeerPairScope()
        ctx = MemoryContext(
            session_id="conv_001",
            sender_agent="alice",
            receiver_agent="bob",
        )
        assert scope.get_scope_key(ctx) == "conv_001:alice:bob"

    def test_two_segment_session_id_extracts_conv_id(self):
        """两段式 session_id 应提取 conversation_id 部分。"""
        scope = PeerPairScope()
        ctx = MemoryContext(
            session_id="conv_001:main",
            sender_agent="alice",
            receiver_agent="bob",
        )
        assert scope.get_scope_key(ctx) == "conv_001:alice:bob"

    def test_fallback_to_agent_id(self):
        """sender_agent 缺失时回退到 agent_id。"""
        scope = PeerPairScope()
        ctx = MemoryContext(
            session_id="conv_001",
            agent_id="agent_x",
            receiver_agent="bob",
        )
        assert scope.get_scope_key(ctx) == "conv_001:agent_x:bob"

    def test_defaults_when_all_missing(self):
        """所有字段缺失时使用默认值。"""
        scope = PeerPairScope()
        ctx = MemoryContext()
        assert scope.get_scope_key(ctx) == "default:unknown:unknown"

    def test_custom_separator(self):
        """自定义分隔符。"""
        scope = PeerPairScope(separator="|")
        ctx = MemoryContext(session_id="conv_001|sender|receiver")
        assert scope.get_scope_key(ctx) == "conv_001|sender|receiver"

    def test_create_key_classmethod(self):
        """便捷类方法构造 key。"""
        key = PeerPairScope.create_key("conv_1", "a", "b")
        assert key == "conv_1:a:b"

    def test_name(self):
        scope = PeerPairScope()
        assert scope.name == "peer_pair"
