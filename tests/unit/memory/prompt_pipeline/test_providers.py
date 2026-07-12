"""Tests for individual SystemPromptProvider implementations."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.memory.prompt_pipeline.providers import (
    BasePromptProvider,
    ExperienceProvider,
    KnowledgeProvider,
    RuntimeProvider,
    SkillProvider,
)


# -- BasePromptProvider --


@pytest.mark.asyncio
async def test_base_prompt_returns_content():
    provider = BasePromptProvider("You are a helpful assistant.")
    result = await provider.get_or_refresh()
    assert result == "You are a helpful assistant."


@pytest.mark.asyncio
async def test_base_prompt_never_refreshes():
    provider = BasePromptProvider("original")
    await provider.get_or_refresh()
    assert provider.last_version == "static"
    result = await provider.get_or_refresh()
    assert result == "original"


@pytest.mark.asyncio
async def test_base_prompt_empty_string():
    provider = BasePromptProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- RuntimeProvider --


@pytest.mark.asyncio
async def test_runtime_contains_date_and_platform():
    provider = RuntimeProvider()
    result = await provider.get_or_refresh()
    assert "Current Time:" in result
    assert "Platform:" in result


@pytest.mark.asyncio
async def test_runtime_version_changes_hourly():
    provider = RuntimeProvider()
    await provider.get_or_refresh()
    assert provider.last_version is not None
    assert len(provider.last_version) == 13  # YYYY-MM-DD-HH


# -- SkillProvider --


@pytest.mark.asyncio
async def test_skill_never_refreshes():
    provider = SkillProvider("skill content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"
    result = await provider.get_or_refresh()
    assert result == "skill content"


@pytest.mark.asyncio
async def test_skill_empty_when_no_content():
    provider = SkillProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- KnowledgeProvider --


@pytest.mark.asyncio
async def test_knowledge_never_refreshes_during_react():
    provider = KnowledgeProvider("knowledge content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"


@pytest.mark.asyncio
async def test_knowledge_empty_when_no_content():
    provider = KnowledgeProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- ExperienceProvider --


@pytest.mark.asyncio
async def test_experience_default_static():
    provider = ExperienceProvider("experience content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"


@pytest.mark.asyncio
async def test_experience_empty_when_no_content():
    provider = ExperienceProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- PeerCommunicationSystemPromptProvider --


def _make_tool_manager(targets: list):
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.multi_agent.bus import AgentMessageBus
    from modex_agent.multi_agent.tools import (
        CommunicationTargetStore,
        SendToAgentTool,
    )

    store = CommunicationTargetStore()
    for t in targets:
        store.add(t)
    tool = SendToAgentTool(
        store=store,
        source=AgentAddress(name="main"),
        broker=MagicMock(),
        registry=MagicMock(),
        agent_bus=MagicMock(spec=AgentMessageBus),
        service=MagicMock(),
    )
    mgr = InMemoryToolManager()
    mgr.register(tool)
    return mgr


class _NoToolManager:
    def get_tool(self, name: str):
        return None


@pytest.mark.asyncio
async def test_peer_comm_provider_no_tool_manager_emits_nothing():
    from modex_agent.memory.prompt_pipeline.providers import (
        PeerCommunicationSystemPromptProvider,
    )

    provider = PeerCommunicationSystemPromptProvider(None)
    result = await provider.get_or_refresh()
    assert result == ""
    assert provider.last_version == "no-remote-comm"


@pytest.mark.asyncio
async def test_peer_comm_provider_no_send_to_agent_tool_emits_nothing():
    from modex_agent.memory.prompt_pipeline.providers import (
        PeerCommunicationSystemPromptProvider,
    )

    provider = PeerCommunicationSystemPromptProvider(_NoToolManager())  # type: ignore[arg-type]
    result = await provider.get_or_refresh()
    assert result == ""


@pytest.mark.asyncio
async def test_peer_comm_provider_only_local_targets_emits_nothing():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        PeerCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets = [
        CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT),
        CommunicationTarget(name="coding", kind=AgentCommKind.NORMAL),
    ]
    provider = PeerCommunicationSystemPromptProvider(_make_tool_manager(targets))
    result = await provider.get_or_refresh()
    assert result == ""
    assert provider.last_version == "no-remote-comm"


@pytest.mark.asyncio
async def test_peer_comm_provider_remote_target_emits_contract():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        PeerCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets = [
        CommunicationTarget(
            name="research-main",
            kind=AgentCommKind.NORMAL,
            bus_ref=MagicMock(),
        ),
    ]
    provider = PeerCommunicationSystemPromptProvider(_make_tool_manager(targets))
    result = await provider.get_or_refresh()
    assert "Remote Agents" in result
    assert "research-main" in result
    assert "send_to_agent" in result
    assert "cannot see" in result.lower()
    assert "optional" in result.lower()
    assert provider.last_version == "remote-comm:research-main"


@pytest.mark.asyncio
async def test_peer_comm_provider_version_sorted_by_name():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        PeerCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets_a = [
        CommunicationTarget(
            name="zeta", kind=AgentCommKind.NORMAL, bus_ref=MagicMock()
        ),
        CommunicationTarget(
            name="alpha", kind=AgentCommKind.NORMAL, bus_ref=MagicMock()
        ),
    ]
    targets_b = list(reversed(targets_a))
    p_a = PeerCommunicationSystemPromptProvider(_make_tool_manager(targets_a))
    p_b = PeerCommunicationSystemPromptProvider(_make_tool_manager(targets_b))
    await p_a.get_or_refresh()
    await p_b.get_or_refresh()
    assert p_a.last_version == p_b.last_version
    assert p_a.last_version == "remote-comm:alpha,zeta"


@pytest.mark.asyncio
async def test_peer_comm_provider_version_changes_when_target_added():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        PeerCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets = [
        CommunicationTarget(
            name="alpha", kind=AgentCommKind.NORMAL, bus_ref=MagicMock()
        ),
    ]
    mgr = _make_tool_manager(targets)
    provider = PeerCommunicationSystemPromptProvider(mgr)
    await provider.get_or_refresh()
    v1 = provider.last_version
    tool = mgr.get_tool("send_to_agent")
    from modex_agent.multi_agent.tools import SendToAgentTool

    assert isinstance(tool, SendToAgentTool)
    tool.add_target(
        CommunicationTarget(
            name="beta", kind=AgentCommKind.NORMAL, bus_ref=MagicMock()
        )
    )
    await provider.get_or_refresh()
    v2 = provider.last_version
    assert v1 != v2
    assert v2 is not None and "beta" in v2


@pytest.mark.asyncio
async def test_peer_comm_provider_contract_does_not_expose_pool_concepts():
    from modex_agent.core.agent import AgentCommKind
    from modex_agent.memory.prompt_pipeline.providers import (
        PeerCommunicationSystemPromptProvider,
    )
    from modex_agent.multi_agent.tools import CommunicationTarget

    targets = [
        CommunicationTarget(
            name="x", kind=AgentCommKind.NORMAL, bus_ref=MagicMock()
        ),
    ]
    provider = PeerCommunicationSystemPromptProvider(_make_tool_manager(targets))
    result = await provider.get_or_refresh()
    low = result.lower()
    assert "pool" not in low
    assert "main agent" not in low
    assert "peer" not in low
    assert "subagent" not in low
