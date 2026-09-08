from __future__ import annotations

from modex_agent.core import AgentCommKind
from modex_agent.multi_agent.tools import SendToAgentTool, TaskDispatchTool


def test_bot_project_new_tool_names_are_available_and_old_names_removed() -> None:
    import modex_agent.multi_agent.tools as tools

    assert SendToAgentTool.__name__ == "SendToAgentTool"
    assert TaskDispatchTool.__name__ == "TaskDispatchTool"
    assert hasattr(tools, "TaskDispatchTool")
    assert not hasattr(tools, "SendMessageTool")
    assert not hasattr(tools, "SendMessageAsyncTool")
    assert not hasattr(tools, "DispatchTaskTool")


def test_agent_comm_kind_is_not_memory_policy() -> None:
    assert AgentCommKind.SUBAGENT.value == "subagent"
    assert not hasattr(AgentCommKind, "EPHEMERAL")
