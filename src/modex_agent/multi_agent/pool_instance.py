"""PoolInstance — runtime container for one agent pool.

Slimmed by Unit E: per-pool data (memory / runtime stores / experience) is
NO LONGER held here. It is owned by the active :class:`Workspace` and resolved
at turn time via ``pool_data[pool_name]``. Only deployment-scoped resources
(providers, managers, broker bridge, communication service) remain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.messaging.broker import AddressKind
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.pool_config.media import MediaConfig
from modex_agent.multi_agent.tools import CommunicationTargetStore
from modex_agent.tools.terminal.managers import TerminalManagerBase

if TYPE_CHECKING:
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
    from modex_agent.multi_agent.session_tree.session_binding import (
        SessionBindingStore,
    )


@dataclass
class PoolInstance:
    """Runtime container for one agent pool's DEPLOYMENT resources.

    Per-pool data (memory system, runtime stores, experience layer) lives on
    the active :class:`Workspace` (``Workspace.pool_data[name]``) and is read
    at turn time — never cached on the pool instance. Shared infra lives in
    BotService.
    """

    name: str
    media: MediaConfig
    subagent_count: int
    pool: Any  # AgentPool
    broker_bridge: Any  # BrokerBridgeService
    tool_manager: Any  # InMemoryToolManager
    skill_manager: Any | None
    mcp_manager: Any | None
    terminal_manager: TerminalManagerBase | None
    root_agent_name: str
    main_execution_strategy: ExecutionStrategyKind
    provider: Any
    notification_service: Any  # AgentNotificationService
    communication_service: (
        Any  # AgentCommunicationService — resolves paths from workspace at runtime
    )
    tree_manager: SessionTreeManager  # exposed for cross-pool peer wiring
    target_store: CommunicationTargetStore  # exposed for cross-pool peer wiring
    session_binding_store: SessionBindingStore | None = None  # tree-level session binding
    requires_main_agent_tools: bool = True  # ADR-0025: mirror of strategy.requires_main_agent_tools
    roster_hook_names: frozenset[str] = frozenset()  # main-agent roster hooks dispatched at assembly (D-A8)
    comm_tools_derived: bool = False
    """Ticket 07: the communication tools were tree-derived at assembly
    (the compiled spec's derived entries resolved through TOOL-slot
    factories). True on the declaration road — the only assembly road;
    the legacy Phase-2 registration it once gated is deleted."""

    @property
    def main_address(self) -> AgentAddress:
        return AgentAddress(kind=AddressKind.AGENT, name=self.root_agent_name)
