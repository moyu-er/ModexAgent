"""TDD tests for RecordScope model + Scope ABC.

Tests the seam: ``RecordScope`` (frozen Pydantic model with ``canonical()``,
``to_path_segment()``, ``merge()``) and the ``Scope`` ABC
(``extract(context) -> RecordScope`` + ``name`` property).

The 8 concrete ``Scope`` subclasses implement ``Scope``; :func:`scope_path_key`
derives a filesystem path segment from a ``Scope`` (via ``extract`` +
``to_path_segment``), and :func:`build_scope` constructs a ``Scope`` from
dimension short-names.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.core.scope import (
    AgentScope,
    ChannelScope,
    ChatScope,
    CompositeScope,
    GlobalScope,
    MemoryContext,
    RecordScope,
    Scope,
    SessionScope,
    TenantScope,
    UserScope,
    build_scope,
    scope_path_key,
)

# ---------------------------------------------------------------------------
# RecordScope
# ---------------------------------------------------------------------------

_ALL_FIELDS = (
    "pool",
    "workspace_id",
    "session_id",
    "session_prefix",
    "agent_id",
    "agent_role",
    "user_id",
    "tenant_id",
    "channel",
    "chat_id",
    "invocation_id",
    "parent_session_id",
)


class TestRecordScopeModel:
    def test_all_fields_default_to_none(self) -> None:
        rs = RecordScope()
        for field in _ALL_FIELDS:
            assert getattr(rs, field) is None, f"{field} should default to None"

    def test_frozen_cannot_mutate(self) -> None:
        rs = RecordScope(session_id="s1")
        with pytest.raises(ValidationError):
            rs.session_id = "s2"

    def test_frozen_cannot_add_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            RecordScope(unknown_field="x")

    def test_canonical_empty_returns_empty_object(self) -> None:
        assert RecordScope().canonical() == "{}"

    def test_canonical_excludes_none_fields(self) -> None:
        rs = RecordScope(session_id="s1", user_id=None)
        canonical = rs.canonical()
        assert "session_id" in canonical
        assert "user_id" not in canonical

    def test_canonical_sorts_keys(self) -> None:
        rs = RecordScope(user_id="u1", session_id="s1", tenant_id="t1")
        # canonical_json sorts keys alphabetically
        assert rs.canonical() == '{"session_id":"s1","tenant_id":"t1","user_id":"u1"}'

    def test_canonical_deterministic_regardless_of_construction_order(self) -> None:
        # Pydantic preserves field definition order, but canonical_json re-sorts.
        rs1 = RecordScope(user_id="u1", session_id="s1")
        rs2 = RecordScope(session_id="s1", user_id="u1")
        assert rs1.canonical() == rs2.canonical()

    def test_canonical_uses_canonical_json(self) -> None:
        from modex_agent.utils.canonical_json import canonical_json

        rs = RecordScope(session_id="s1", user_id="u1")
        assert rs.canonical() == canonical_json(rs.model_dump(exclude_none=True))


class TestRecordScopeToPathSegment:
    def test_no_dimensions_returns_empty(self) -> None:
        # mirrors GlobalScope's empty-key behavior
        assert RecordScope().to_path_segment() == ""
        assert RecordScope(session_id="s1").to_path_segment() == ""

    def test_single_dimension_with_value(self) -> None:
        rs = RecordScope(session_id="s1")
        assert rs.to_path_segment("session") == "s1"

    def test_single_dimension_none_returns_default(self) -> None:
        rs = RecordScope()
        assert rs.to_path_segment("session") == "default"

    def test_multiple_dimensions_join_with_colon(self) -> None:
        rs = RecordScope(user_id="u1", session_id="s1")
        assert rs.to_path_segment("user", "session") == "u1:s1"

    def test_multiple_dimensions_with_none_uses_default(self) -> None:
        rs = RecordScope()
        assert rs.to_path_segment("user", "session") == "default:default"

    def test_partial_none_dimensions(self) -> None:
        rs = RecordScope(user_id="u1", session_id=None)
        assert rs.to_path_segment("user", "session") == "u1:default"

    def test_all_twelve_dimensions_accepted(self) -> None:
        rs = RecordScope(
            pool="p1",
            workspace_id="w1",
            session_id="s1",
            session_prefix="sp1",
            agent_id="a1",
            agent_role="main",
            user_id="u1",
            tenant_id="t1",
            channel="c1",
            chat_id="ch1",
            invocation_id="i1",
            parent_session_id="ps1",
        )
        result = rs.to_path_segment(
            "pool",
            "workspace",
            "session",
            "session_prefix",
            "agent",
            "agent_role",
            "user",
            "tenant",
            "channel",
            "chat",
            "invocation",
            "parent_session",
        )
        assert result == "p1:w1:s1:sp1:a1:main:u1:t1:c1:ch1:i1:ps1"

    def test_unknown_dimension_raises_value_error(self) -> None:
        rs = RecordScope()
        with pytest.raises(ValueError):
            rs.to_path_segment("nonexistent")


class TestRecordScopeMerge:
    def test_other_overrides_self_non_none(self) -> None:
        self_rs = RecordScope(session_id="old", user_id="u1")
        other = RecordScope(session_id="new")
        merged = self_rs.merge(other)
        assert merged.session_id == "new"
        assert merged.user_id == "u1"  # kept from self

    def test_self_kept_when_other_none(self) -> None:
        self_rs = RecordScope(session_id="s1")
        other = RecordScope(session_id=None)
        merged = self_rs.merge(other)
        assert merged.session_id == "s1"

    def test_both_none_stays_none(self) -> None:
        self_rs = RecordScope()
        other = RecordScope()
        merged = self_rs.merge(other)
        for field in _ALL_FIELDS:
            assert getattr(merged, field) is None

    def test_returns_new_instance(self) -> None:
        self_rs = RecordScope(session_id="s1")
        other = RecordScope(user_id="u1")
        merged = self_rs.merge(other)
        assert merged is not self_rs
        assert merged is not other
        # originals unchanged
        assert self_rs.session_id == "s1"
        assert self_rs.user_id is None
        assert other.user_id == "u1"
        assert other.session_id is None

    def test_merge_combines_disjoint_fields(self) -> None:
        self_rs = RecordScope(session_id="s1")
        other = RecordScope(user_id="u1")
        merged = self_rs.merge(other)
        assert merged.session_id == "s1"
        assert merged.user_id == "u1"

    def test_merge_chains(self) -> None:
        a = RecordScope(session_id="s1")
        b = RecordScope(user_id="u1")
        c = RecordScope(tenant_id="t1")
        merged = a.merge(b).merge(c)
        assert merged.session_id == "s1"
        assert merged.user_id == "u1"
        assert merged.tenant_id == "t1"


# ---------------------------------------------------------------------------
# Scope ABC
# ---------------------------------------------------------------------------


class TestScopeABC:
    def test_cannot_instantiate_abstract(self) -> None:
        with pytest.raises(TypeError):
            Scope()

    def test_subclass_must_implement_extract(self) -> None:
        class Incomplete(Scope):
            @property
            def name(self) -> str:
                return "incomplete"

        with pytest.raises(TypeError):
            Incomplete()

    def test_subclass_must_implement_name(self) -> None:
        class Incomplete(Scope):
            def extract(self, context: MemoryContext) -> RecordScope:
                return RecordScope()

        with pytest.raises(TypeError):
            Incomplete()


# ---------------------------------------------------------------------------
# Concrete Scope subclasses — extract + name + path key
# ---------------------------------------------------------------------------


class TestSessionScope:
    def test_extract_with_value(self) -> None:
        rs = SessionScope().extract(MemoryContext(session_id="sess_123"))
        assert rs.session_id == "sess_123"
        # only session_id should be set
        assert rs.user_id is None
        assert rs.tenant_id is None

    def test_extract_none_returns_none_field(self) -> None:
        rs = SessionScope().extract(MemoryContext())
        assert rs.session_id is None

    def test_name(self) -> None:
        assert SessionScope().name == "session"

    def test_path_key_with_value(self) -> None:
        assert scope_path_key(SessionScope(), MemoryContext(session_id="sess_123")) == "sess_123"

    def test_path_key_default(self) -> None:
        assert scope_path_key(SessionScope(), MemoryContext()) == "default"


class TestUserScope:
    def test_extract_with_value(self) -> None:
        rs = UserScope().extract(MemoryContext(user_id="user_123"))
        assert rs.user_id == "user_123"

    def test_extract_none(self) -> None:
        rs = UserScope().extract(MemoryContext())
        assert rs.user_id is None

    def test_name(self) -> None:
        assert UserScope().name == "user"

    def test_path_key(self) -> None:
        assert scope_path_key(UserScope(), MemoryContext(user_id="user_123")) == "user_123"
        assert scope_path_key(UserScope(), MemoryContext()) == "default"


class TestTenantScope:
    def test_extract_with_value(self) -> None:
        rs = TenantScope().extract(MemoryContext(tenant_id="tenant_123"))
        assert rs.tenant_id == "tenant_123"

    def test_name(self) -> None:
        assert TenantScope().name == "tenant"

    def test_path_key(self) -> None:
        assert scope_path_key(TenantScope(), MemoryContext(tenant_id="tenant_123")) == "tenant_123"
        assert scope_path_key(TenantScope(), MemoryContext()) == "default"


class TestAgentScope:
    def test_extract_with_value(self) -> None:
        rs = AgentScope().extract(
            MemoryContext(agent_id="agent_123", agent_role="subagent")
        )
        assert rs.agent_id == "agent_123"
        assert rs.agent_role == "subagent"

    def test_name(self) -> None:
        assert AgentScope().name == "agent:agent_role"

    def test_path_key(self) -> None:
        context = MemoryContext(agent_id="agent_123", agent_role="subagent")
        assert scope_path_key(AgentScope(), context) == "agent_123:subagent"
        assert scope_path_key(AgentScope(), MemoryContext()) == "default:default"


class TestChannelScope:
    def test_extract_with_value(self) -> None:
        rs = ChannelScope().extract(MemoryContext(channel="qq"))
        assert rs.channel == "qq"

    def test_name(self) -> None:
        assert ChannelScope().name == "channel"

    def test_path_key(self) -> None:
        assert scope_path_key(ChannelScope(), MemoryContext(channel="qq")) == "qq"
        assert scope_path_key(ChannelScope(), MemoryContext()) == "default"


class TestChatScope:
    def test_extract_with_value(self) -> None:
        rs = ChatScope().extract(MemoryContext(chat_id="group_123"))
        assert rs.chat_id == "group_123"

    def test_name(self) -> None:
        assert ChatScope().name == "chat"

    def test_path_key(self) -> None:
        assert scope_path_key(ChatScope(), MemoryContext(chat_id="group_123")) == "group_123"
        assert scope_path_key(ChatScope(), MemoryContext()) == "default"


class TestGlobalScope:
    def test_extract_returns_empty_record(self) -> None:
        rs = GlobalScope().extract(MemoryContext())
        for field in _ALL_FIELDS:
            assert getattr(rs, field) is None

    def test_extract_ignores_context(self) -> None:
        ctx = MemoryContext(session_id="s1", user_id="u1")
        rs = GlobalScope().extract(ctx)
        for field in _ALL_FIELDS:
            assert getattr(rs, field) is None

    def test_name(self) -> None:
        assert GlobalScope().name == "global"

    def test_path_key_returns_empty(self) -> None:
        assert scope_path_key(GlobalScope(), MemoryContext()) == ""
        assert scope_path_key(GlobalScope(), MemoryContext(session_id="s1")) == ""

    def test_canonical_is_empty_object(self) -> None:
        assert GlobalScope().extract(MemoryContext()).canonical() == "{}"


# ---------------------------------------------------------------------------
# CompositeScope — uses RecordScope.merge
# ---------------------------------------------------------------------------


class TestCompositeScope:
    def test_extract_merges_two_scopes(self) -> None:
        composite = CompositeScope(UserScope(), SessionScope())
        ctx = MemoryContext(user_id="u1", session_id="s1")
        rs = composite.extract(ctx)
        assert rs.user_id == "u1"
        assert rs.session_id == "s1"

    def test_extract_merges_three_scopes(self) -> None:
        composite = CompositeScope(TenantScope(), UserScope(), SessionScope())
        ctx = MemoryContext(tenant_id="t1", user_id="u1", session_id="s1")
        rs = composite.extract(ctx)
        assert rs.tenant_id == "t1"
        assert rs.user_id == "u1"
        assert rs.session_id == "s1"

    def test_extract_equivalent_to_manual_merge(self) -> None:
        ctx = MemoryContext(user_id="u1", session_id="s1")
        composite = CompositeScope(UserScope(), SessionScope())
        manual = UserScope().extract(ctx).merge(SessionScope().extract(ctx))
        assert composite.extract(ctx) == manual

    def test_extract_with_none_values(self) -> None:
        composite = CompositeScope(UserScope(), SessionScope())
        rs = composite.extract(MemoryContext())
        assert rs.user_id is None
        assert rs.session_id is None

    def test_extract_with_global_scope(self) -> None:
        composite = CompositeScope(GlobalScope(), UserScope())
        ctx = MemoryContext(user_id="u1")
        rs = composite.extract(ctx)
        # global contributes nothing; user contributes user_id
        assert rs.user_id == "u1"

    def test_name_joins_subscope_names(self) -> None:
        composite = CompositeScope(UserScope(), SessionScope())
        assert composite.name == "user:session"

    def test_name_three_scopes(self) -> None:
        composite = CompositeScope(TenantScope(), UserScope(), SessionScope())
        assert composite.name == "tenant:user:session"

    def test_path_key_two_scopes(self) -> None:
        composite = CompositeScope(UserScope(), SessionScope())
        ctx = MemoryContext(user_id="u1", session_id="s1")
        assert scope_path_key(composite, ctx) == "u1:s1"

    def test_path_key_three_scopes(self) -> None:
        composite = CompositeScope(TenantScope(), UserScope(), SessionScope())
        ctx = MemoryContext(tenant_id="t1", user_id="u1", session_id="s1")
        assert scope_path_key(composite, ctx) == "t1:u1:s1"

    def test_path_key_with_defaults(self) -> None:
        composite = CompositeScope(UserScope(), SessionScope())
        assert scope_path_key(composite, MemoryContext()) == "default:default"

    def test_repr(self) -> None:
        composite = CompositeScope(UserScope(), SessionScope())
        assert "CompositeScope" in repr(composite)
        assert "user" in repr(composite)
        assert "session" in repr(composite)


# ---------------------------------------------------------------------------
# build_scope factory
# ---------------------------------------------------------------------------


class TestBuildScope:
    def test_single_string_autowraps(self) -> None:
        scope = build_scope("user")
        assert isinstance(scope, UserScope)

    def test_single_element_list_returns_leaf(self) -> None:
        scope = build_scope(["user"])
        assert isinstance(scope, UserScope)

    def test_multiple_dims_returns_composite(self) -> None:
        scope = build_scope(["user", "session"])
        assert isinstance(scope, CompositeScope)
        assert len(scope.scopes) == 2
        assert isinstance(scope.scopes[0], UserScope)
        assert isinstance(scope.scopes[1], SessionScope)

    def test_empty_list_returns_global(self) -> None:
        scope = build_scope([])
        assert isinstance(scope, GlobalScope)

    def test_global_dim_returns_global(self) -> None:
        scope = build_scope(["global"])
        assert isinstance(scope, GlobalScope)

    def test_global_and_user_returns_composite(self) -> None:
        scope = build_scope(["global", "user"])
        assert isinstance(scope, CompositeScope)
        assert isinstance(scope.scopes[0], GlobalScope)
        assert isinstance(scope.scopes[1], UserScope)

    def test_unknown_dim_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            build_scope(["nonexistent"])

    def test_unknown_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            build_scope("nonexistent")

    def test_preserves_order_in_composite(self) -> None:
        scope = build_scope(["tenant", "user", "session"])
        assert isinstance(scope, CompositeScope)
        names = [s.name for s in scope.scopes]
        assert names == ["tenant", "user", "session"]

    def test_all_seven_leaf_dimensions(self) -> None:
        for dim in ["session", "user", "tenant", "agent", "channel", "chat", "global"]:
            scope = build_scope([dim])
            # each should produce a usable Scope (leaf or global)
            assert isinstance(scope, Scope)


# ---------------------------------------------------------------------------
# All 8 concrete subclasses satisfy the Scope interface
# ---------------------------------------------------------------------------


class TestConcreteScopeInterface:
    _CONCRETE_CLASSES = [
        SessionScope,
        UserScope,
        TenantScope,
        AgentScope,
        ChannelScope,
        ChatScope,
        GlobalScope,
        CompositeScope,
    ]

    def test_all_concrete_scopes_are_scope_subclasses(self) -> None:
        for cls in self._CONCRETE_CLASSES:
            assert issubclass(cls, Scope), f"{cls.__name__} should be a Scope subclass"

    def test_path_key_preserves_segment_behavior_all_leafs(self) -> None:
        ctx_full = MemoryContext(
            session_id="s1",
            user_id="u1",
            tenant_id="t1",
            agent_id="a1",
            agent_role="subagent",
            channel="c1",
            chat_id="ch1",
        )
        ctx_empty = MemoryContext()

        assert scope_path_key(SessionScope(), ctx_full) == "s1"
        assert scope_path_key(SessionScope(), ctx_empty) == "default"
        assert scope_path_key(UserScope(), ctx_full) == "u1"
        assert scope_path_key(UserScope(), ctx_empty) == "default"
        assert scope_path_key(TenantScope(), ctx_full) == "t1"
        assert scope_path_key(AgentScope(), ctx_full) == "a1:subagent"
        assert scope_path_key(ChannelScope(), ctx_full) == "c1"
        assert scope_path_key(ChannelScope(), ctx_empty) == "default"
        assert scope_path_key(ChatScope(), ctx_full) == "ch1"
        assert scope_path_key(ChatScope(), ctx_empty) == "default"
        assert scope_path_key(GlobalScope(), ctx_full) == ""
        assert scope_path_key(GlobalScope(), ctx_empty) == ""
