"""PoolInstance — runtime container for one agent pool."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PoolInstance:
    """Runtime container for one agent pool.

    All components are pool-private. Shared infra lives in BotService.
    """

    name: str
    config: Any  # PoolConfig
    pool: Any  # AgentPool
    broker_bridge: Any  # BrokerBridgeService
    memory_system: Any
    context_manager: Any
    tool_manager: Any  # InMemoryToolManager
    skill_manager: Any | None
    mcp_manager: Any | None
    terminal_manager: Any | None
    main_agent_name: str
    provider: Any
    notification_service: Any  # AgentNotificationService
    communication_service: Any  # AgentCommunicationService — updated on workspace switch

    @property
    def main_address(self):
        from framework.multi_agent.address import AgentAddress
        return AgentAddress(kind="agent", name=self.main_agent_name)
