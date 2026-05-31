from __future__ import annotations

from pathlib import Path

from framework.ioc.configs.agent import AgentConfig
from framework.ioc.configs.app import AppConfig
from framework.ioc.configs.llm import LLMConfig
from framework.ioc.factories.descriptors import build_subagent_descriptor
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.tools import SendToAgentTool
from framework.multi_agent.template_registry import AgentTemplateRegistry

_BOT_PROJECT_DIR = Path(__file__).resolve().parent.parent


def test_bot_project_subagents_are_configured_as_subagents() -> None:
    """Subagent templates for office-expert and query-12306 must be loadable."""
    registry = AgentTemplateRegistry(_BOT_PROJECT_DIR)
    templates = registry.list_templates("main")
    agent_types = {t.agent_type for t in templates}

    assert "office-expert" in agent_types, (
        "office-expert subagent template should exist in config/pools/main/templates/"
    )
    assert "query-12306" in agent_types, (
        "query-12306 subagent template should exist in config/pools/main/templates/"
    )


def test_bot_project_new_tool_names_are_available_and_old_names_removed() -> None:
    import framework.multi_agent.tools as tools

    assert SendToAgentTool.__name__ == "SendToAgentTool"
    assert not hasattr(tools, "SendMessageTool")
    assert not hasattr(tools, "SendMessageAsyncTool")
    assert not hasattr(tools, "DispatchTaskTool")


def test_agent_comm_kind_is_not_memory_policy() -> None:
    assert AgentCommKind.SUBAGENT.value == "subagent"
    assert not hasattr(AgentCommKind, "EPHEMERAL")


async def test_bot_project_subagent_builder_preserves_subagent_comm_kind(tmp_path) -> None:
    descriptor, _tools, _skills, _memory = await build_subagent_descriptor(
        AgentConfig(name="query-12306", role="subagent", standard_tools=False),
        AppConfig(llm=LLMConfig(model="test-model")),
        tmp_path,
        tmp_path / "memory",
        safety=None,
        llm=None,
    )

    assert descriptor.comm_kind == AgentCommKind.SUBAGENT


async def test_bot_project_subagent_descriptor_does_not_deny_communication_tools(tmp_path) -> None:
    from framework.ioc.factories.descriptors import build_subagent_descriptor

    descriptor, _tools, _skills, _memory = await build_subagent_descriptor(
        AgentConfig(name="office-expert", role="subagent"),
        AppConfig(llm=LLMConfig(model="test-model")),
        tmp_path,
        tmp_path / "memory",
        safety=None,
        llm=None,
    )

    denied = descriptor.denied_tools or []
    assert "send_to_agent" not in denied


async def test_bot_project_subagent_builder_does_not_register_target_listing_without_runtime(tmp_path) -> None:
    _descriptor, tools, _skills, _memory = await build_subagent_descriptor(
        AgentConfig(name="query-12306", role="subagent", standard_tools=False),
        AppConfig(llm=LLMConfig(model="test-model")),
        tmp_path,
        tmp_path / "memory",
        safety=None,
        llm=None,
    )

    assert tools.get_tool("list_communication_targets") is None
