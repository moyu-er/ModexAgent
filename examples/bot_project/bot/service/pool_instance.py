"""PoolInstance — runtime container for one agent pool.

Slimmed by Unit E: per-pool data (memory / runtime stores / experience) is
NO LONGER held here. It is owned by the active :class:`Workspace` and resolved
at turn time via ``pool_data[pool_name]``. Only deployment-scoped resources
(providers, managers, broker bridge, communication service) remain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PoolInstance:
    """Runtime container for one agent pool's DEPLOYMENT resources.

    Per-pool data (memory system, runtime stores, experience layer) lives on
    the active :class:`Workspace` (``Workspace.pool_data[name]``) and is read
    at turn time — never cached on the pool instance. Shared infra lives in
    BotService.
    """

    name: str
    config: Any  # PoolConfig
    pool: Any  # AgentPool
    broker_bridge: Any  # BrokerBridgeService
    tool_manager: Any  # InMemoryToolManager
    skill_manager: Any | None
    mcp_manager: Any | None
    terminal_manager: Any | None
    main_agent_name: str
    provider: Any
    notification_service: Any  # AgentNotificationService
    communication_service: Any  # AgentCommunicationService — resolves paths from workspace at runtime

    @property
    def main_address(self):
        from modex_agent.multi_agent.address import AgentAddress

        return AgentAddress(kind="agent", name=self.main_agent_name)
