"""Derived communication tool factories (SPEC §5.2, ticket 07).

The three communication tools converge on ONE registration path: the
ScopeCompiler injects the derived entries (``task`` / ``send_to_agent`` /
``send_to_peer``) into the compiled spec's ``tools``, and these TOOL-slot
factories resolve them at assembly time — never a roster declaration,
never a materialize-time side registration. This generalizes the existing
FW per-subagent pattern (``AgentTemplate._register_send_to_agent``) to
all three tools.

The factories read the pool-layer
:class:`~modex_agent.plugins.assembly.context.CommunicationFacilities`
from the context chain (populated by ``PoolAssembleStage`` from
``SupplyInfra.communication`` on the declaration road; by
``AgentTemplate.materialize`` for subagents). The legacy roster road is
deleted — these derived entries are the only registration path.
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
    from modex_agent.plugins.assembly.context import CommunicationFacilities


class _ToolConfig(BaseModel):
    """Empty config for the communication tool factories."""

    model_config = {"frozen": True, "extra": "forbid"}


def _facilities(ctx: AgentContext) -> CommunicationFacilities:
    """The pool-layer communication facilities, loudly when absent."""
    pool_runtime = ctx.pool_runtime
    facilities = pool_runtime.communication if pool_runtime is not None else None
    if facilities is None:
        raise ValueError(
            "derived communication tool entry requires pool-layer "
            "communication facilities in PoolRuntimeDeps — the scope "
            "declaration road wires them at pool assembly (ticket 07)"
        )
    return facilities


class TaskToolFactory(ComponentFactory):
    """``task`` — subagent dispatch for agents with declared children.

    The per-agent target store's SUBAGENT entries (the direct children,
    SPEC §3.2) are the tool's target list.
    """

    config_model = _ToolConfig

    async def create(self, config: BaseModel, ctx: AgentContext) -> Tool:
        del config
        facilities = _facilities(ctx)
        store = facilities.target_store
        if store is None:
            raise ValueError(
                "derived 'task' entry requires a per-agent target store "
                "(agents with declared children)"
            )
        return TaskDispatchTool(
            store=store,
            source=AgentAddress(name=ctx.agent_name),
            service=facilities.service,
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
        facilities = _facilities(ctx)
        store = facilities.target_store
        if store is None:
            raise ValueError(
                "derived 'send_to_peer' entry requires a per-agent target "
                "store (roots with declared peer links)"
            )
        return SendToPeerTool(
            store=store,
            source=AgentAddress(name=ctx.agent_name),
            service=facilities.service,
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
        facilities = _facilities(ctx)
        return SendToAgentTool(
            store=CommunicationTargetStore(for_subagent=True),
            source=AgentAddress(name=ctx.agent_name),
            service=facilities.service,
        )


def register_default_communication_tools(ctx: PluginRegistrationContext) -> None:
    """Register the derived communication TOOL factories by name."""
    ctx.register_tool("task", TaskToolFactory())
    ctx.register_tool("send_to_agent", SendToAgentToolFactory())
    ctx.register_tool("send_to_peer", SendToPeerToolFactory())
