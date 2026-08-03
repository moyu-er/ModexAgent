# ruff: noqa: ANN401
"""`AgentNode` + `AgentNodeFactory` — wrap an `Agent` instance as a `Node`.

Ticket 02 (P3.1): the business-layer `Node` that wraps a complete agent
call. ``AgentNode.execute`` constructs an ``AgentContext`` from the
``GraphContext`` (via an injected factory function), creates a
``CollectorEmitter``, calls ``await agent.run(agent_ctx, emitter)``, and
delivers the ``AgentResult`` to the next node via ``self.deliver``.

Dual-input model (ticket 02 §2):

- Input 1 (trigger): graph state — upstream submit writes. The
  ``integrated_input`` parameter carries upstream payloads.
- Input 2 (execution): inbox messages — the agent's own ``InboxFlushHook``
  pulls these during its react loop. The graph layer is unaware of inbox;
  ``AgentNode`` does NOT wire it.

Dependency injection (ticket 02 §3):

- ``AgentNode`` holds a reference to an ``Agent`` instance (injected at
  construction by the bot factory).
- ``AgentContext`` construction is delegated to an injected factory function
  ``agent_context_factory: Callable[[GraphContext], AgentContext]``. This
  keeps ``AgentNode`` generic — not tied to ReAct-specific context assembly.

Pattern mirrors ``FunctionNode`` (the reference ``Node`` implementation).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from modex_agent.core.agent import Agent, AgentContext
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.events import EmitterConfig
from modex_graph.context import GraphContext
from modex_graph.integration import IntegratedInput
from modex_graph.node import Node
from modex_graph.node_factory import NodeFactory
from modex_graph.result import NodeResult
from modex_graph.spec import NodeSpec


class CollectorEmitter(ContentEmitter[Any]):
    """Simple emitter that collects events and buffers content.

    Created by ``AgentNode`` to serve as a sink for agent events. The
    agent's final output is captured in the ``AgentResult`` (returned from
    ``agent.run()`` and delivered to the next node). This emitter prevents
    crashes and stores events for inspection / debugging.

    Mirrors the buffering pattern of ``_BufferingEmitter`` (test helper)
    and ``SummarizerTrajectoryEmitter`` — both subclass
    ``ContentEmitter[Any]`` and accumulate content / events.
    """

    def __init__(self, config: EmitterConfig | None = None) -> None:
        super().__init__(config)
        self.content_buffer: str = ""
        self.reasoning_buffer: str = ""
        self.events: list[tuple[Any, Any]] = []
        self.result: AgentResult | None = None
        self.error: str | None = None

    async def _on_event(self, event: Any, data: Any = None) -> None:
        self.events.append((event, data))

    async def emit_delta(self, delta: str) -> None:
        if delta:
            self.content_buffer += delta

    async def emit_content(self, full_content: str) -> None:
        if full_content:
            self.content_buffer += full_content

    async def emit_complete(self, result: AgentResult) -> None:
        self.result = result

    async def emit_error(self, error: str) -> None:
        self.error = error


class AgentNode(Node[Any]):
    """Wraps an ``Agent`` instance as a graph ``Node`` (ticket 02).

    ``execute`` constructs an ``AgentContext`` from the ``GraphContext``
    via the injected factory, creates a ``CollectorEmitter``, calls
    ``await agent.run(agent_ctx, emitter)``, and delivers the
    ``AgentResult`` to ``next_node`` via ``self.deliver``.

    The node is stateless per execution — the agent holds its own state
    via ``AgentContext``. No ``NodeState`` is needed.
    """

    def __init__(
        self,
        agent: Agent[Any],
        agent_context_factory: Callable[[GraphContext[Any]], AgentContext],
        *,
        next_node: str | None = None,
    ) -> None:
        """Initialize the agent wrapper.

        Args:
            agent: the ``Agent`` instance to wrap. Injected at construction
                by the bot factory.
            agent_context_factory: constructs an ``AgentContext`` from the
                ``GraphContext``. This is business wiring — ``AgentNode``
                itself does not know how to build ``AgentContext``.
            next_node: the explicit deliver target. If ``None``,
                ``_resolve_default_target`` resolves via topology (P3.4b.2).
        """
        self._agent = agent
        self._agent_context_factory = agent_context_factory
        self._next_node = next_node

    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> NodeResult:
        """Construct context, run the agent, deliver the result."""
        agent_ctx = self._agent_context_factory(ctx)
        emitter = CollectorEmitter()
        agent_ctx.emitter = emitter
        result = await self._agent.run(agent_ctx, emitter)
        self.deliver(result, self._next_node, ctx)
        return NodeResult()


class AgentNodeFactory(NodeFactory):
    """Creates ``AgentNode`` from an agent registry (ticket 02).

    Holds a registry of agent instances by name plus their matching
    ``agent_context_factory`` callables. ``NodeSpec.config =
    {"agent": "<name>", "next_node": "<target>" (optional)}``.

    Pattern mirrors ``FunctionNodeFactory``: the factory holds a
    name→resource mapping, validates config in ``create()``, and returns
    ``None`` from ``config_schema()``.
    """

    def __init__(
        self,
        agents: dict[str, Agent[Any]] | None = None,
        context_factories: dict[str, Callable[[GraphContext[Any]], AgentContext]] | None = None,
    ) -> None:
        """Initialize with optional pre-populated registries.

        Args:
            agents: initial name→agent mapping. May be ``None`` (empty);
                use ``register_agent`` to add entries after construction.
            context_factories: initial name→factory mapping. Must cover the
                same keys as ``agents``. May be ``None`` (empty).
        """
        self._agents: dict[str, Agent[Any]] = (
            dict(agents) if agents is not None else {}
        )
        self._context_factories: dict[str, Callable[[GraphContext[Any]], AgentContext]] = (
            dict(context_factories) if context_factories is not None else {}
        )

    def register_agent(
        self,
        name: str,
        agent: Agent[Any],
        context_factory: Callable[[GraphContext[Any]], AgentContext],
    ) -> None:
        """Register an agent + its context factory under ``name``.

        Raises:
            ValueError: if ``name`` is already registered (no silent override).
        """
        if name in self._agents:
            raise ValueError(
                f"Agent {name!r} is already registered. "
                f"Use a different name or unregister first."
            )
        self._agents[name] = agent
        self._context_factories[name] = context_factory

    def unregister_agent(self, name: str) -> None:
        """Remove ``name`` from both registries. No-op if not registered."""
        self._agents.pop(name, None)
        self._context_factories.pop(name, None)

    def create(self, spec: NodeSpec) -> Node[Any]:
        """Create an ``AgentNode`` from the spec's ``agent`` config key.

        Raises:
            ValueError: if ``config["agent"]`` is missing, not a string,
                or not a registered agent name; or if the matching context
                factory is missing; or if ``next_node`` is not a string.
        """
        agent_name = spec.config.get("agent")
        if not agent_name or not isinstance(agent_name, str):
            raise ValueError(
                f"AgentNode requires an 'agent' config key (string). "
                f"Got: {agent_name!r}. Spec: {spec!r}."
            )
        agent = self._agents.get(agent_name)
        if agent is None:
            raise ValueError(
                f"Agent {agent_name!r} is not registered. "
                f"Registered agents: {sorted(self._agents.keys())}."
            )
        context_factory = self._context_factories.get(agent_name)
        if context_factory is None:
            raise ValueError(
                f"No context factory registered for agent {agent_name!r}. "
                f"Use register_agent(name, agent, context_factory) to "
                f"register both together."
            )
        next_node = spec.config.get("next_node")
        if next_node is not None and not isinstance(next_node, str):
            raise ValueError(
                f"AgentNode 'next_node' config must be a string or None. Got: {next_node!r}."
            )
        return AgentNode(agent, context_factory, next_node=next_node)

    def config_schema(self) -> type[BaseModel] | None:
        """No Pydantic schema — config is validated in ``create()``."""
        return None


__all__ = ["AgentNode", "AgentNodeFactory", "CollectorEmitter"]
