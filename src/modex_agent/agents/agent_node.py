"""Graph node base class for agent-backed scheduling."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, assert_never

from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.session_registry import SessionRegistry
from modex_graph.compiled_graph import CompiledGraph
from modex_graph.context import GraphContext
from modex_graph.node import Node
from modex_graph.utils.id import generate_id


class SessionStrategy(Enum):
    """Control how an agent node allocates sessions across invocations."""

    CACHED = "cached"
    PER_INVOCATION = "per_invocation"


class AgentNode(Node[Any], ABC):
    """Base graph node with agent session lifecycle management."""

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
        return "[not found]"


__all__ = ["AgentNode", "SessionStrategy"]
