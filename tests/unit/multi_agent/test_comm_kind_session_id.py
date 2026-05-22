"""Tests for AgentCommKind, AgentSessionMeta, and descriptor/profile changes."""

from __future__ import annotations

import pytest

from framework.core.agent import AgentSessionMeta
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.descriptor import AgentDescriptor, AgentLLMConfig
from framework.multi_agent.registry import AgentProfile
from framework.multi_agent.address import AgentAddress


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
        assert meta.uuid is None

    def test_subagent_session_meta_with_uuid(self) -> None:
        meta = AgentSessionMeta(
            conversation_id="conv-1",
            agent_name="office-expert",
            comm_kind=AgentCommKind.SUBAGENT,
            uuid="a1b2c3d4e5f6",
        )
        assert meta.comm_kind == AgentCommKind.SUBAGENT
        assert meta.uuid == "a1b2c3d4e5f6"

    def test_session_meta_is_frozen(self) -> None:
        meta = AgentSessionMeta(
            conversation_id="conv-1",
            agent_name="main",
            comm_kind=AgentCommKind.NORMAL,
        )
        with pytest.raises(Exception):
            meta.uuid = "abc"  # type: ignore[misc]


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
