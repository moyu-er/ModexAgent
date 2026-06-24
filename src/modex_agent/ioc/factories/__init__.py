"""Factory functions that consume Pydantic configs and produce runtime objects.

All factories are pure functions: AppConfig in, runtime objects out.
No framework code should import from bot_project.
"""

from modex_agent.ioc.factories.agent import create_agent
from modex_agent.ioc.factories.descriptors import build_subagent_descriptor
from modex_agent.ioc.factories.governance import create_governance, create_subagent_governance
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.ioc.factories.memory import create_memory
from modex_agent.ioc.factories.tools import (
    connect_mcp,
    create_tool_manager,
    register_mcp_tools,
)

__all__ = [
    "build_subagent_descriptor",
    "connect_mcp",
    "create_agent",
    "create_governance",
    "create_llm_provider",
    "create_memory",
    "create_subagent_governance",
    "create_tool_manager",
    "register_mcp_tools",
]
