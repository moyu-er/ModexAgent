"""Derived communication tool factories (SPEC §5.2, ticket 07).

The three communication tools converge on ONE registration path: the
ScopeCompiler injects the derived entries (``task`` / ``send_to_agent`` /
``send_to_peer``) into the compiled spec's ``tools``, and these TOOL-slot
factories resolve them at assembly time — never a roster declaration,
never a materialize-time side registration. This generalizes the existing
FW per-subagent pattern (``AgentTemplate._register_send_to_agent``) to
all three tools.

The factories read the pool's ``subagents`` capability faces from the
context chain (the retired ``CommunicationFacilities`` typed carrier
died with the supply convergence, SPEC §8.4): the pool-level
:class:`~modex_agent.multi_agent.communication.AgentCommunicationService`
from ``capability_supply['subagents']`` and the PER-AGENT target store
from the capability's wiring artifacts
(``capability_wirings['subagents']`` — populated by
``SubagentsCapability.assemble`` before tool resolution).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from modex_agent.core.tool_manager import Tool
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.tools import (
    CommunicationTargetStore,
    SendToAgentTool,
    SendToPeerTool,
    TaskDispatchTool,
)
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import AgentContext
from modex_agent.plugins.loader import PluginRegistrationContext

if TYPE_CHECKING:
    from modex_agent.plugins.defaults.capabilities.subagents import SubagentsSupply


class _ToolConfig(BaseModel):
    """Empty config for the communication tool factories."""

    model_config = {"frozen": True, "extra": "forbid"}


def _service(ctx: AgentContext) -> SubagentsSupply:
    """The pool's subagents supply (the communication service carrier)."""
    from modex_agent.plugins.defaults.capabilities.subagents import (
        require_subagents_supply,
    )

    return require_subagents_supply(ctx.pool_runtime)


def _target_store(ctx: AgentContext) -> CommunicationTargetStore:
    """The per-agent communication target store from the ``subagents``
    wiring artifacts — loudly when absent."""
    wirings = ctx.capability_wirings
    wiring = wirings.get("subagents") if wirings is not None else None
    store = wiring.artifacts.get("target_store") if wiring is not None else None
    if store is None:
        raise ValueError(
            "derived communication entry requires the per-agent target "
            "store from the 'subagents' capability's wiring artifacts "
            "(capability_wirings['subagents']) — the capability assembles "
            "it for child-carrying agents and pool roots; a hand-referenced "
            "communication entry without the capability cannot resolve"
        )
    return store


class TaskToolFactory(ComponentFactory):
    """``task`` — subagent dispatch for agents with declared children.

    The per-agent target store's SUBAGENT entries (the direct children,
    SPEC §3.2) are the tool's target list.
    """

    config_model = _ToolConfig

    async def create(self, config: BaseModel, ctx: AgentContext) -> Tool:
        del config
        supply = _service(ctx)
        store = _target_store(ctx)
        return TaskDispatchTool(
            store=store,
            source=AgentAddress(name=ctx.agent_name),
            service=supply.service,
        )


class SendToPeerToolFactory(ComponentFactory):
    """``send_to_peer`` — peer messaging for roots with declared links.

    Peer NORMAL targets (with tree references) join the same per-agent
    store; the FW resolution service supplies them at workspace
    materialize time (ticket 13).
    """

    config_model = _ToolConfig

    async def create(self, config: BaseModel, ctx: AgentContext) -> Tool:
        del config
        supply = _service(ctx)
        store = _target_store(ctx)
        return SendToPeerTool(
            store=store,
            source=AgentAddress(name=ctx.agent_name),
            service=supply.service,
        )


class SendToAgentToolFactory(ComponentFactory):
    """``send_to_agent`` — subagent→parent consultation (non-root).

    Mirrors the baked per-subagent default: the parent is resolved
    dynamically at execution time from ``current_agent_context``, so the
    store is a fresh subagent-mode store with no static targets.
    """

    config_model = _ToolConfig

    async def create(self, config: BaseModel, ctx: AgentContext) -> Tool:
        del config
        supply = _service(ctx)
        return SendToAgentTool(
            store=CommunicationTargetStore(for_subagent=True),
            source=AgentAddress(name=ctx.agent_name),
            service=supply.service,
        )


def register_default_communication_tools(ctx: PluginRegistrationContext) -> None:
    """Register the derived communication TOOL factories by name."""
    ctx.register_tool("task", TaskToolFactory())
    ctx.register_tool("send_to_agent", SendToAgentToolFactory())
    ctx.register_tool("send_to_peer", SendToPeerToolFactory())
