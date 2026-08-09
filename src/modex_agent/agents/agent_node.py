"""Graph node base class for agent-backed scheduling."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, Any, assert_never

from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.session_registry import SessionRegistry
from modex_graph.compiled_graph import CompiledGraph
from modex_graph.constants import DeliverConsumptionStatus, FrameworkPayloadSource
from modex_graph.context import GraphContext
from modex_graph.integration import IntegratedInput, IntegratedPayload
from modex_graph.node import Node
from modex_graph.utils.id import generate_id

if TYPE_CHECKING:
    from modex_graph.persistence.graph_metadata import InvocationContext
    from modex_graph.persistence.persistence_coordinator import (
        GraphPersistenceCoordinator,
    )


class SessionStrategy(Enum):
    """Control how an agent node allocates sessions across invocations."""

    CACHED = "cached"
    PER_INVOCATION = "per_invocation"


class AgentNode(Node[Any], ABC):
    """Base graph node with agent session lifecycle management.

    Overrides ``_integrate_upstream`` to always filter ``CONSUMED_PENDING``
    delivers. Agent session memory (ReAct MessageStore) persists upstream
    input as SYSTEM_REMINDER across invocations — crash recovery must not
    re-consume already-injected delivers, or the agent's session history
    would contain duplicate input messages.

    See ADR-0038 decision 5 for the full rationale.
    """

    DESCRIPTION_NOT_FOUND = "[not found]"

    def __init__(
        self,
        *,
        session_strategy: SessionStrategy = SessionStrategy.CACHED,
    ) -> None:
        self._session_strategy = session_strategy
        self._session: SessionInfo | None = None
        self._graph_ref: CompiledGraph[Any] | None = None

    @abstractmethod
    def agent_name(self) -> str:
        """Return the name used to bind this node's agent session."""
        ...

    @abstractmethod
    async def _resolve_session_registry(self) -> SessionRegistry:
        ...

    async def _ensure_session(self, ctx: GraphContext[Any]) -> SessionInfo:
        match self._session_strategy:
            case SessionStrategy.CACHED:
                if self._session is None:
                    self._session = await self._create_session(ctx)
                return self._session
            case SessionStrategy.PER_INVOCATION:
                return await self._create_session(ctx)
            case unreachable:
                assert_never(unreachable)

    async def _create_session(self, ctx: GraphContext[Any]) -> SessionInfo:
        agent_name = self.agent_name()
        match self._session_strategy:
            case SessionStrategy.CACHED:
                external_id = f"{self.node_id}.{agent_name}"
            case SessionStrategy.PER_INVOCATION:
                external_id = f"{self.node_id}.{agent_name}.{generate_id()}"
            case unreachable:
                assert_never(unreachable)
        session = SessionIdFactory().create(agent_name, external_id=external_id)
        registry = await self._resolve_session_registry()
        await registry.register(session)
        return session

    def resolve_description(self) -> str:
        """Return the agent description exposed to graph tooling."""
        return AgentNode.DESCRIPTION_NOT_FOUND

    def _integrate_upstream(
        self,
        coordinator: GraphPersistenceCoordinator,
        invocation: InvocationContext,
        *,
        resume_snapshot: dict[str, Any] | None,
    ) -> IntegratedInput:
        """Collect upstream delivers with agent-memory-aware filtering.

        Agent node session memory persists upstream input across invocations.
        ``CONSUMED_PENDING`` delivers (consumed by a prior crashed invocation)
        are always filtered — re-injecting them would duplicate the
        SYSTEM_REMINDER in the agent's session history.

        On crash recovery with no new PENDING delivers, this returns an empty
        ``IntegratedInput``. The agent runs with its existing session memory
        (which already contains the upstream input from the crashed attempt).
        """
        is_resume = resume_snapshot is not None
        delivers = coordinator.collect_consumable_delivers(
            self.node_id, invocation.invocation_id
        )
        delivers = [
            d for d in delivers
            if d.status == DeliverConsumptionStatus.PENDING
        ]
        if delivers:
            coordinator.mark_delivers_consumed(
                self.node_id,
                [r.deliver_id for r in delivers],
                invocation.invocation_id,
            )
            payloads = [
                IntegratedPayload(
                    source_node=r.source_node_id,
                    content=r.content,
                )
                for r in delivers
            ]
        else:
            payloads = []
        if is_resume:
            payloads = [
                IntegratedPayload(
                    source_node=FrameworkPayloadSource.RESUME,
                    content=resume_snapshot,
                )
            ] + payloads
        return self.input_integrator.integrate(payloads)


__all__ = ["AgentNode", "SessionStrategy"]
