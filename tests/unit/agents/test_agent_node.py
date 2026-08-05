# ruff: noqa: ANN401
"""Tests for ``AgentNode`` + ``AgentNodeFactory``.

Covers:

- ``AgentNode`` construction (agent + factory + next_node).
- ``execute()`` calls ``agent_context_factory(ctx)``, creates a
  ``CollectorEmitter``, calls ``agent.run(ctx, emitter)``, and delivers
  the ``AgentResult`` to ``next_node``.
- ``CollectorEmitter`` buffers content / stores events / captures result.
- ``AgentNodeFactory``: creates from config, holds agent + context_factory
  registries, rejects bad config.
- Factory ``register_agent`` / ``unregister_agent`` lifecycle.
- Integration with ``NodeRegistry`` (name + trigger set by registry).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.agents.agent_node import AgentNode, AgentNodeFactory, CollectorEmitter
from modex_agent.core.agent import Agent, AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_graph import (
    GraphContext,
    GraphNode,
    GraphPersistenceCoordinator,
    GraphRuntime,
    GraphState,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
    NodeTrigger,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
    NullNodeStateStore,
)


class _AutoRegCoord(GraphPersistenceCoordinator):
    """Test-only coordinator that auto-registers nodes on begin_invocation."""

    def collect_consumable_delivers(
        self, node_name: str, invocation_id: int
    ) -> list[Any]:
        if self.get_deliver_store(node_name) is None:
            self.register_node(node_name)
        return super().collect_consumable_delivers(node_name, invocation_id)

    def route_deliver(
        self,
        target_node: str,
        content: Any,
        source_node: str,
        source_invocation_id: int,
    ) -> int | None:
        if target_node != GraphNode.END and self.get_deliver_store(target_node) is None:
            self.register_node(target_node)
        return super().route_deliver(target_node, content, source_node, source_invocation_id)


def _make_coordinator() -> _AutoRegCoord:
    return _AutoRegCoord(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=NullNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )


# ── Test helpers ──────────────────────────────────────────────────────────


def _make_ctx() -> GraphContext[Any]:
    """Build a GraphContext with a bare GraphState + no-op runtime."""
    return GraphContext(
        state=GraphState(),
        runtime=GraphRuntime(),
        coordinator=_make_coordinator(),
    )


def _make_agent_context(ctx: GraphContext[Any]) -> AgentContext:
    """Build a minimal real AgentContext for testing."""
    return AgentContext(
        system_prompt="test",
        history=MagicMock(),
        tool_manager=MagicMock(),
        session=MagicMock(),
    )


class _MockAgent(Agent[Any]):
    """Mock agent that records calls and returns a fixed AgentResult."""

    def __init__(
        self,
        agent_name: str = "mock",
        result: AgentResult | None = None,
    ) -> None:
        self._name = agent_name
        self._result = result or AgentResult(
            content="done",
            stop_reason=StopReason.COMPLETED,
        )
        self.run_calls: list[tuple[AgentContext, ContentEmitter[Any]]] = []

    @property
    def name(self) -> str:
        return self._name

    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[Any],
    ) -> AgentResult:
        self.run_calls.append((context, emitter))
        return self._result


# ── CollectorEmitter ──────────────────────────────────────────────────────


class TestCollectorEmitter:
    async def test_buffers_delta_content(self) -> None:
        emitter = CollectorEmitter()
        await emitter.emit_delta("hello ")
        await emitter.emit_delta("world")
        assert emitter.content_buffer == "hello world"

    async def test_buffers_full_content(self) -> None:
        emitter = CollectorEmitter()
        await emitter.emit_content("full content")
        assert emitter.content_buffer == "full content"

    async def test_stores_complete_result(self) -> None:
        emitter = CollectorEmitter()
        result = AgentResult(content="final", stop_reason=StopReason.COMPLETED)
        await emitter.emit_complete(result)
        assert emitter.result is result

    async def test_stores_error(self) -> None:
        emitter = CollectorEmitter()
        await emitter.emit_error("something broke")
        assert emitter.error == "something broke"

    async def test_collects_events(self) -> None:
        emitter = CollectorEmitter()
        await emitter.emit("model_output", "text")
        await emitter.emit("tool_call_start", {"tool": "search"})
        assert len(emitter.events) == 2
        assert emitter.events[0] == ("model_output", "text")
        assert emitter.events[1] == ("tool_call_start", {"tool": "search"})

    async def test_no_op_emit_stream_end(self) -> None:
        """emit_stream_end is a no-op on the base ContentEmitter."""
        emitter = CollectorEmitter()
        await emitter.emit_stream_end(resuming=False)
        # Should not raise and should not alter buffers.
        assert emitter.content_buffer == ""

    async def test_initial_state(self) -> None:
        emitter = CollectorEmitter()
        assert emitter.content_buffer == ""
        assert emitter.reasoning_buffer == ""
        assert emitter.events == []
        assert emitter.result is None
        assert emitter.error is None


# ── AgentNode construction ────────────────────────────────────────────────


class TestAgentNodeConstruction:
    def test_construction_stores_agent_and_factory(self) -> None:
        agent = _MockAgent()
        node = AgentNode(agent, _make_agent_context, next_node="target")
        assert isinstance(node, Node)
        assert node._agent is agent
        assert node._agent_context_factory is _make_agent_context
        assert node._next_node == "target"

    def test_next_node_defaults_to_none(self) -> None:
        node = AgentNode(_MockAgent(), _make_agent_context)
        assert node._next_node is None

    def test_name_defaults_to_empty(self) -> None:
        node = AgentNode(_MockAgent(), _make_agent_context)
        assert node.name == ""


# ── AgentNode execute + deliver ───────────────────────────────────────────


class TestAgentNodeExecute:
    async def test_calls_agent_run_and_delivers_result(self) -> None:
        result = AgentResult(content="output", stop_reason=StopReason.COMPLETED)
        agent = _MockAgent(result=result)
        node = AgentNode(agent, _make_agent_context, next_node="downstream")
        node.name = "agent_n"
        ctx = _make_ctx()
        await node.run(ctx)
        # Agent.run was called once.
        assert len(agent.run_calls) == 1
        # Result was delivered to "downstream".
        assert node._submit_result == {"downstream": [result]}

    async def test_factory_receives_graph_context(self) -> None:
        agent = _MockAgent()
        factory = MagicMock(side_effect=_make_agent_context)
        node = AgentNode(agent, factory, next_node="out")
        node.name = "agent_n"
        ctx = _make_ctx()
        await node.run(ctx)
        factory.assert_called_once_with(ctx)

    async def test_emitter_set_on_agent_context(self) -> None:
        agent = _MockAgent()
        node = AgentNode(agent, _make_agent_context, next_node="out")
        node.name = "agent_n"
        ctx = _make_ctx()
        await node.run(ctx)
        agent_ctx, emitter = agent.run_calls[0]
        assert isinstance(emitter, CollectorEmitter)
        assert agent_ctx.emitter is emitter

    async def test_execute_returns_none(self) -> None:
        agent = _MockAgent()
        node = AgentNode(agent, _make_agent_context, next_node="target")
        node.name = "agent_n"
        ctx = _make_ctx()
        result = await node.run(ctx)
        assert result is None

    async def test_deliver_to_end_sentinel(self) -> None:
        from modex_graph import GraphNode

        agent = _MockAgent()
        node = AgentNode(agent, _make_agent_context, next_node=GraphNode.END)
        node.name = "agent_end"
        ctx = _make_ctx()
        await node.run(ctx)
        assert GraphNode.END in node._submit_result

    async def test_error_result_is_delivered(self) -> None:
        """AgentResult with error is still delivered (agent handles errors)."""
        error_result = AgentResult(
            error="boom",
            stop_reason=StopReason.ERROR,
        )
        agent = _MockAgent(result=error_result)
        node = AgentNode(agent, _make_agent_context, next_node="err_target")
        node.name = "agent_n"
        ctx = _make_ctx()
        await node.run(ctx)
        assert node._submit_result == {"err_target": [error_result]}

    async def test_collector_emitter_captures_events_during_run(self) -> None:
        """The CollectorEmitter captures events emitted by the agent."""

        class _EmittingAgent(Agent[Any]):
            @property
            def name(self) -> str:
                return "emitting"

            async def run(
                self,
                context: AgentContext,
                emitter: ContentEmitter[Any],
            ) -> AgentResult:
                await emitter.emit_delta("partial ")
                await emitter.emit_delta("content")
                await emitter.emit_complete(
                    AgentResult(content="final", stop_reason=StopReason.COMPLETED)
                )
                return AgentResult(content="final", stop_reason=StopReason.COMPLETED)

        node = AgentNode(_EmittingAgent(), _make_agent_context, next_node="out")
        node.name = "emit_n"
        ctx = _make_ctx()
        await node.run(ctx)
        # The delivered result is the AgentResult returned by run().
        delivered = node._submit_result["out"][0]
        assert isinstance(delivered, AgentResult)
        assert delivered.content == "final"


# ── AgentNodeFactory ──────────────────────────────────────────────────────


class TestAgentNodeFactory:
    def test_create_from_config(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"my_agent": agent},
            context_factories={"my_agent": _make_agent_context},
        )
        node = factory.create(
            NodeSpec(
                name="n1",
                node_type="agent",
                config={"agent": "my_agent", "next_node": "out"},
            )
        )
        assert isinstance(node, AgentNode)
        assert node._agent is agent
        assert node._agent_context_factory is _make_agent_context
        assert node._next_node == "out"

    def test_create_without_next_node(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"my_agent": agent},
            context_factories={"my_agent": _make_agent_context},
        )
        node = factory.create(NodeSpec(name="n1", node_type="agent", config={"agent": "my_agent"}))
        assert isinstance(node, AgentNode)
        assert node._next_node is None

    def test_create_returns_node_subclass(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"a": agent},
            context_factories={"a": _make_agent_context},
        )
        node = factory.create(NodeSpec(name="n1", node_type="agent", config={"agent": "a"}))
        assert isinstance(node, Node)

    def test_register_agent(self) -> None:
        factory = AgentNodeFactory()
        agent = _MockAgent()
        factory.register_agent("new_agent", agent, _make_agent_context)
        node = factory.create(NodeSpec(name="n1", node_type="agent", config={"agent": "new_agent"}))
        assert isinstance(node, AgentNode)
        assert node._agent is agent

    def test_register_agent_duplicate_raises(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"dup": agent},
            context_factories={"dup": _make_agent_context},
        )
        with pytest.raises(ValueError, match="already registered"):
            factory.register_agent("dup", agent, _make_agent_context)

    def test_unregister_agent(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"rm": agent},
            context_factories={"rm": _make_agent_context},
        )
        factory.unregister_agent("rm")
        with pytest.raises(ValueError, match="not registered"):
            factory.create(NodeSpec(name="n1", node_type="agent", config={"agent": "rm"}))

    def test_unregister_noop_if_not_registered(self) -> None:
        factory = AgentNodeFactory()
        factory.unregister_agent("nonexistent")

    def test_config_schema_returns_none(self) -> None:
        factory = AgentNodeFactory()
        assert factory.config_schema() is None

    def test_empty_registry_by_default(self) -> None:
        factory = AgentNodeFactory()
        with pytest.raises(ValueError, match="not registered"):
            factory.create(NodeSpec(name="n1", node_type="agent", config={"agent": "anything"}))

    def test_initial_dicts_are_copied(self) -> None:
        agent = _MockAgent()
        original_agents: dict[str, Agent[Any]] = {"a": agent}
        original_factories: dict[str, Any] = {"a": _make_agent_context}
        factory = AgentNodeFactory(
            agents=original_agents,
            context_factories=original_factories,
        )
        factory.unregister_agent("a")
        assert "a" in original_agents
        assert "a" in original_factories

    def test_factory_is_node_factory_subclass(self) -> None:
        assert issubclass(AgentNodeFactory, NodeFactory)


# ── Factory rejects bad config ────────────────────────────────────────────


class TestAgentNodeFactoryRejectsBadConfig:
    def test_missing_agent_key(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"a": agent},
            context_factories={"a": _make_agent_context},
        )
        with pytest.raises(ValueError, match="requires an 'agent'"):
            factory.create(NodeSpec(name="n1", node_type="agent", config={}))

    def test_unregistered_agent_name(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"a": agent},
            context_factories={"a": _make_agent_context},
        )
        with pytest.raises(ValueError, match="not registered"):
            factory.create(NodeSpec(name="n1", node_type="agent", config={"agent": "nonexistent"}))

    def test_non_string_agent_name(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"a": agent},
            context_factories={"a": _make_agent_context},
        )
        with pytest.raises(ValueError, match="requires an 'agent'"):
            factory.create(NodeSpec(name="n1", node_type="agent", config={"agent": 123}))

    def test_non_string_next_node(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"a": agent},
            context_factories={"a": _make_agent_context},
        )
        with pytest.raises(ValueError, match="next_node"):
            factory.create(
                NodeSpec(
                    name="n1",
                    node_type="agent",
                    config={"agent": "a", "next_node": 123},
                )
            )

    def test_missing_context_factory(self) -> None:
        """Agent registered but context factory missing — should raise."""
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"a": agent},
            context_factories={},  # no factory for "a"
        )
        with pytest.raises(ValueError, match="No context factory"):
            factory.create(NodeSpec(name="n1", node_type="agent", config={"agent": "a"}))


# ── Integration with NodeRegistry ─────────────────────────────────────────


class TestAgentNodeRegistryIntegration:
    def test_registry_sets_name_from_spec(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"my_agent": agent},
            context_factories={"my_agent": _make_agent_context},
        )
        registry = NodeRegistry()
        registry.register("agent", factory)
        node = registry.create(
            NodeSpec(name="my_node", node_type="agent", config={"agent": "my_agent"})
        )
        assert isinstance(node, AgentNode)
        assert node.name == "my_node"

    def test_registry_applies_trigger_override(self) -> None:
        agent = _MockAgent()
        factory = AgentNodeFactory(
            agents={"a": agent},
            context_factories={"a": _make_agent_context},
        )
        registry = NodeRegistry()
        registry.register("agent", factory)
        node = registry.create(
            NodeSpec(
                name="n",
                node_type="agent",
                config={"agent": "a"},
                trigger=NodeTrigger.ON_RECEIVE,
            )
        )
        assert node.trigger == NodeTrigger.ON_RECEIVE

    async def test_registry_create_then_execute(self) -> None:
        result = AgentResult(content="hello", stop_reason=StopReason.COMPLETED)
        agent = _MockAgent(result=result)
        factory = AgentNodeFactory(
            agents={"a": agent},
            context_factories={"a": _make_agent_context},
        )
        registry = NodeRegistry()
        registry.register("agent", factory)
        node = registry.create(
            NodeSpec(
                name="agent_node",
                node_type="agent",
                config={"agent": "a", "next_node": "out"},
            )
        )
        ctx = _make_ctx()
        await node.run(ctx)
        assert node._submit_result == {"out": [result]}
        assert len(agent.run_calls) == 1


# ── Export check ──────────────────────────────────────────────────────────


class TestExports:
    def test_exports_from_agents_package(self) -> None:
        import modex_agent.agents as agents_pkg

        assert agents_pkg.AgentNode is AgentNode
        assert agents_pkg.AgentNodeFactory is AgentNodeFactory
        assert agents_pkg.CollectorEmitter is CollectorEmitter
