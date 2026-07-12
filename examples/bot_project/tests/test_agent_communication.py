from __future__ import annotations

from pathlib import Path

from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.pool_config import PoolStore
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import SendToAgentTool

_BOT_PROJECT_DIR = Path(__file__).resolve().parent.parent


def test_bot_project_subagents_are_configured_as_subagents() -> None:
    """Subagent templates for office-expert must be loadable in the default pool."""
    registry = AgentTemplateRegistry(PoolStore(base_dir=_BOT_PROJECT_DIR))
    templates = registry.list_templates("default")
    agent_types = {t.spec.agent_name for t in templates}

    assert "office-expert" in agent_types, (
        "office-expert subagent template should exist in config/pools/default/templates/"
    )


def test_bot_project_new_tool_names_are_available_and_old_names_removed() -> None:
    import modex_agent.multi_agent.tools as tools

    assert SendToAgentTool.__name__ == "SendToAgentTool"
    assert not hasattr(tools, "SendMessageTool")
    assert not hasattr(tools, "SendMessageAsyncTool")
    assert not hasattr(tools, "DispatchTaskTool")


def test_agent_comm_kind_is_not_memory_policy() -> None:
    assert AgentCommKind.SUBAGENT.value == "subagent"
    assert not hasattr(AgentCommKind, "EPHEMERAL")
