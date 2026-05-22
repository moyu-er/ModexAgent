"""Tests for AgentCommKind, AgentSessionMeta, session ID strategy, and descriptor/profile changes."""

from __future__ import annotations

import pytest

from framework.core.agent import AgentSessionMeta
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.descriptor import AgentDescriptor, AgentLLMConfig
from framework.multi_agent.registry import AgentProfile
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.session_id import AgentSessionParts, DefaultSessionIdStrategy


class TestAgentCommKind:
    def test_enum_values(self) -> None:
        assert AgentCommKind.NORMAL == "normal"
        assert AgentCommKind.SUBAGENT == "subagent"

    def test_enum_is_strenum(self) -> None:
        assert isinstance(AgentCommKind.NORMAL, str)


class TestAgentSessionMeta:
    def test_normal_session_meta(self) -> None:
        meta = AgentSessionMeta(
            conversation_id="conv-1",
            agent_name="main",
            comm_kind=AgentCommKind.NORMAL,
        )
        assert meta.conversation_id == "conv-1"
        assert meta.agent_name == "main"
        assert meta.comm_kind == AgentCommKind.NORMAL
        assert meta.invocation_id is None

    def test_subagent_session_meta_with_uuid(self) -> None:
        meta = AgentSessionMeta(
            conversation_id="conv-1",
            agent_name="office-expert",
            comm_kind=AgentCommKind.SUBAGENT,
            invocation_id="a1b2c3d4e5f6",
        )
        assert meta.comm_kind == AgentCommKind.SUBAGENT
        assert meta.invocation_id == "a1b2c3d4e5f6"

    def test_session_meta_is_frozen(self) -> None:
        meta = AgentSessionMeta(
            conversation_id="conv-1",
            agent_name="main",
            comm_kind=AgentCommKind.NORMAL,
        )
        with pytest.raises(Exception):
            meta.invocation_id = "abc"  # type: ignore[misc]


class TestAgentDescriptorCommKind:
    def test_default_comm_kind_is_normal(self) -> None:
        desc = AgentDescriptor(address=AgentAddress(name="test"))
        assert desc.comm_kind == AgentCommKind.NORMAL

    def test_subagent_comm_kind(self) -> None:
        desc = AgentDescriptor(
            address=AgentAddress(name="office-expert"),
            comm_kind=AgentCommKind.SUBAGENT,
        )
        assert desc.comm_kind == AgentCommKind.SUBAGENT


class TestAgentProfileCommKind:
    def test_default_comm_kind_is_normal(self) -> None:
        profile = AgentProfile(name="test")
        assert profile.comm_kind == AgentCommKind.NORMAL

    def test_subagent_profile_comm_kind(self) -> None:
        profile = AgentProfile(
            name="office-expert",
            comm_kind=AgentCommKind.SUBAGENT,
        )
        assert profile.comm_kind == AgentCommKind.SUBAGENT


class TestSessionIdStrategyFormat:
    def test_normal_format(self) -> None:
        strategy = DefaultSessionIdStrategy()
        sid = strategy.format(conversation_id="conv-1", agent_name="main")
        assert sid == "conv-1:main"

    def test_subagent_format_with_uuid(self) -> None:
        strategy = DefaultSessionIdStrategy()
        sid = strategy.format(conversation_id="conv-1", agent_name="office-expert", invocation_id="a1b2c3")
        assert sid == "conv-1:office-expert:a1b2c3"

    def test_format_rejects_empty_conversation_id(self) -> None:
        strategy = DefaultSessionIdStrategy()
        with pytest.raises(ValueError):
            strategy.format(conversation_id="", agent_name="main")

    def test_format_rejects_empty_agent_name(self) -> None:
        strategy = DefaultSessionIdStrategy()
        with pytest.raises(ValueError):
            strategy.format(conversation_id="conv-1", agent_name="")

    def test_format_rejects_empty_uuid(self) -> None:
        strategy = DefaultSessionIdStrategy()
        with pytest.raises(ValueError):
            strategy.format(conversation_id="conv-1", agent_name="office-expert", invocation_id="")


class TestSessionIdStrategyParse:
    def test_parse_two_part(self) -> None:
        strategy = DefaultSessionIdStrategy()
        parts = strategy.parse("conv-1:main")
        assert parts.conversation_id == "conv-1"
        assert parts.agent_name == "main"
        assert parts.invocation_id is None

    def test_parse_three_part(self) -> None:
        strategy = DefaultSessionIdStrategy()
        parts = strategy.parse("conv-1:office-expert:a1b2c3")
        assert parts.conversation_id == "conv-1"
        assert parts.agent_name == "office-expert"
        assert parts.invocation_id == "a1b2c3"

    def test_parse_round_trip_normal(self) -> None:
        strategy = DefaultSessionIdStrategy()
        sid = strategy.format(conversation_id="conv-1", agent_name="main")
        parts = strategy.parse(sid)
        assert parts.conversation_id == "conv-1"
        assert parts.agent_name == "main"
        assert parts.invocation_id is None

    def test_parse_round_trip_subagent(self) -> None:
        strategy = DefaultSessionIdStrategy()
        sid = strategy.format(conversation_id="conv-1", agent_name="office-expert", invocation_id="a1b2c3")
        parts = strategy.parse(sid)
        assert parts.conversation_id == "conv-1"
        assert parts.agent_name == "office-expert"
        assert parts.invocation_id == "a1b2c3"

    def test_parse_rejects_one_part(self) -> None:
        strategy = DefaultSessionIdStrategy()
        with pytest.raises(ValueError):
            strategy.parse("conv1")

    def test_parse_rejects_four_part(self) -> None:
        strategy = DefaultSessionIdStrategy()
        with pytest.raises(ValueError):
            strategy.parse("a:b:c:d")

    def test_parse_rejects_empty_uuid_segment(self) -> None:
        strategy = DefaultSessionIdStrategy()
        with pytest.raises(ValueError):
            strategy.parse("conv-1:office-expert:")
