from __future__ import annotations

from typing import Any

import anyio
from pydantic import BaseModel

from modex_agent.orchestration import GraphOrchestrator
from modex_graph import (
    DefaultGraphState,
    EdgeSpec,
    GraphContext,
    GraphNode,
    GraphRuntime,
    GraphSpec,
    InMemoryGraphInstanceStore,
    InMemoryGraphSpecStore,
    IntegratedInput,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
    create_null_coordinator,
)


class _BlockingNode(Node[DefaultGraphState]):
    def __init__(self, entered: anyio.Event, release: anyio.Event) -> None:
        self._entered = entered
        self._release = release

    async def execute(
        self,
        ctx: GraphContext[DefaultGraphState],
        integrated_input: IntegratedInput,
    ) -> None:
        self._entered.set()
        await self._release.wait()
        self.deliver(None, None, ctx)


class _BlockingFactory(NodeFactory):
    def __init__(self, entered: anyio.Event, release: anyio.Event) -> None:
        self._entered = entered
        self._release = release

    def create(self, spec: NodeSpec) -> Node[Any]:
        return _BlockingNode(self._entered, self._release)

    def config_schema(self) -> type[BaseModel] | None:
        return None


def _make_orchestrator(
    node_registry: NodeRegistry | None = None,
) -> tuple[GraphOrchestrator, InMemoryGraphSpecStore]:
    spec_store = InMemoryGraphSpecStore()
    orchestrator = GraphOrchestrator(
        node_registry=node_registry if node_registry is not None else NodeRegistry(),
        state_classes={"default": DefaultGraphState},
        spec_store=spec_store,
        instance_store=InMemoryGraphInstanceStore(),
    )
    return orchestrator, spec_store


def _blocking_spec() -> GraphSpec:
    return GraphSpec(
        name="blocking_graph",
        nodes=[NodeSpec(name="entry", node_type="blocking")],
        edges=[
            EdgeSpec(source=GraphNode.START, target="entry"),
            EdgeSpec(source="entry", target=GraphNode.END),
        ],
        state_class="default",
    )


def test_get_graph_context_returns_none_for_unknown_instance() -> None:
    orchestrator, _ = _make_orchestrator()

    context = orchestrator.get_graph_context(999999)

    assert context is None


async def test_get_graph_context_returns_context_during_run() -> None:
    entered = anyio.Event()
    release = anyio.Event()
    registry = NodeRegistry()
    registry.register("blocking", _BlockingFactory(entered, release))
    orchestrator, spec_store = _make_orchestrator(registry)
    graph_instance_id = await orchestrator.create_instance(spec_store.save(_blocking_spec()))

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(orchestrator.run_instance, graph_instance_id)
        await entered.wait()

        context = orchestrator.get_graph_context(graph_instance_id)

        assert context is not None
        assert context.graph_instance_id == graph_instance_id
        release.set()


async def test_get_graph_context_returns_none_after_finalize() -> None:
    entered = anyio.Event()
    release = anyio.Event()
    release.set()
    registry = NodeRegistry()
    registry.register("blocking", _BlockingFactory(entered, release))
    orchestrator, spec_store = _make_orchestrator(registry)
    graph_instance_id = await orchestrator.create_instance(spec_store.save(_blocking_spec()))

    await orchestrator.run_instance(graph_instance_id)

    assert orchestrator.get_graph_context(graph_instance_id) is None


def test_graph_context_user_data_stores_node_artifacts() -> None:
    context = GraphContext(
        state=DefaultGraphState(),
        runtime=GraphRuntime(),
        coordinator=create_null_coordinator(1),
        graph_instance_id=1,
    )
    context.user_data = {}
    artifacts = "graph-turn-artifacts"

    context.user_data.setdefault("node_artifacts", {})["entry"] = artifacts

    assert context.user_data["node_artifacts"]["entry"] == artifacts
    assert context.user_data["node_artifacts"].get("unknown") is None
