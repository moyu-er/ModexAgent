"""Factory functions that consume Pydantic configs and produce runtime objects.

All factories are pure functions: AppConfig in, runtime objects out.
No framework code should import from bot_project.
"""

from framework.ioc.factories.agent import create_agent
from framework.ioc.factories.app import App, create_app
from framework.ioc.factories.compression import create_peer_compression_coordinator
from framework.ioc.factories.descriptors import (
    build_peer_descriptor,
    build_subagent_descriptor,
)
from framework.ioc.factories.governance import create_governance, create_peer_governance
from framework.ioc.factories.llm import create_llm_provider
from framework.ioc.factories.memory import create_memory
from framework.ioc.factories.tools import (
    connect_mcp,
    create_tool_manager,
    register_mcp_tools,
)

__all__ = [
    "App",
    "build_peer_descriptor",
    "build_subagent_descriptor",
    "connect_mcp",
    "create_agent",
    "create_app",
    "create_governance",
    "create_llm_provider",
    "create_memory",
    "create_peer_compression_coordinator",
    "create_peer_governance",
    "create_tool_manager",
    "register_mcp_tools",
]
