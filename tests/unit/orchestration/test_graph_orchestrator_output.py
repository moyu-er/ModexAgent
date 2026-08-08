from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from modex_agent.orchestration import GraphOrchestrator
from modex_graph import (
    DefaultGraphState,
    EdgeSpec,
    GraphContext,
    GraphNode,
    GraphOutput,
    GraphOutputAdapter,
    GraphOutputKind,
    GraphPayload,
    GraphSpec,
    InMemoryGraphInstanceStore,
    InMemoryGraphSpecStore,
    IntegratedInput,
    LinearScheduler,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
    NullGraphInstanceStore,
)


class _RecordingOutputAdapter(GraphOutputAdapter):
    def __init__(self) -> None:
        self.outputs: list[GraphOutput] = []

    async def emit(self, output: GraphOutput) -> None:
        self.outputs.append(output)


class _GraphCrashError(RuntimeError):
    pass


class _CrashingNode(Node[DefaultGraphState]):
    async def execute(
        self,
        ctx: GraphContext[DefaultGraphState],
        integrated_input: IntegratedInput,
    ) -> None:
        raise _GraphCrashError("boom")


class _CrashingNodeFactory(NodeFactory):
    def create(self, spec: NodeSpec) -> Node[Any]:
        return _CrashingNode()

    def config_schema(self) -> type[BaseModel] | None:
        return None


async def test_empty_graph_emits_user_input_as_completed_result() -> None:
    spec_store = InMemoryGraphSpecStore()
    instance_store = InMemoryGraphInstanceStore()
    output_adapter = _RecordingOutputAdapter()
    node_registry = NodeRegistry()
    orchestrator = GraphOrchestrator(
        node_registry=node_registry,
        state_classes={"default": DefaultGraphState},
        spec_store=spec_store,
        instance_store=instance_store,
        output_adapter=output_adapter,
    )
    spec_id = spec_store.save(
        GraphSpec(
            name="empty",
            nodes=[],
            edges=[EdgeSpec(source=GraphNode.START, target=GraphNode.END)],
            state_class="default",
        )
    )
    user_input = GraphPayload(content="hello")

    graph_instance_id = await orchestrator.create_and_run(spec_id, user_input=user_input)

    assert output_adapter.outputs == [
        GraphOutput(
            kind=GraphOutputKind.COMPLETED,
            graph_instance_id=graph_instance_id,
            result=[user_input],
        )
    ]


async def test_crashed_graph_emits_output_before_reraising() -> None:
    spec_store = InMemoryGraphSpecStore()
    instance_store = InMemoryGraphInstanceStore()
    output_adapter = _RecordingOutputAdapter()
    node_registry = NodeRegistry()
    node_registry.register("crashing", _CrashingNodeFactory())
    orchestrator = GraphOrchestrator(
        node_registry=node_registry,
        state_classes={"default": DefaultGraphState},
        spec_store=spec_store,
        instance_store=instance_store,
        output_adapter=output_adapter,
    )
    spec_id = spec_store.save(
        GraphSpec(
            name="crashing",
            nodes=[NodeSpec(name="entry", node_type="crashing")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="default",
        )
    )

    with pytest.raises(_GraphCrashError, match="boom"):
        await orchestrator.create_and_run(spec_id)

    assert len(output_adapter.outputs) == 1
    output = output_adapter.outputs[0]
    assert output.kind is GraphOutputKind.CRASHED
    assert output.error == "boom"


async def test_completed_output_uses_state_returned_by_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_store = InMemoryGraphSpecStore()
    instance_store = InMemoryGraphInstanceStore()
    output_adapter = _RecordingOutputAdapter()
    recovered_result = [GraphPayload(content="recovered")]

    async def run_with_recovered_state(
        self: LinearScheduler[DefaultGraphState],
        ctx: GraphContext[DefaultGraphState],
    ) -> DefaultGraphState:
        final_state = DefaultGraphState(result=recovered_result)
        ctx.state = final_state
        return final_state

    monkeypatch.setattr(LinearScheduler, "run_async", run_with_recovered_state)
    orchestrator = GraphOrchestrator(
        node_registry=NodeRegistry(),
        state_classes={"default": DefaultGraphState},
        spec_store=spec_store,
        instance_store=instance_store,
        output_adapter=output_adapter,
    )
    spec_id = spec_store.save(
        GraphSpec(
            name="recovering",
            nodes=[],
            edges=[EdgeSpec(source=GraphNode.START, target=GraphNode.END)],
            state_class="default",
        )
    )

    await orchestrator.create_and_run(spec_id)

    assert output_adapter.outputs[0].result == recovered_result


async def test_completed_instance_is_evicted_with_null_instance_store() -> None:
    spec_store = InMemoryGraphSpecStore()
    orchestrator = GraphOrchestrator(
        node_registry=NodeRegistry(),
        state_classes={"default": DefaultGraphState},
        spec_store=spec_store,
        instance_store=NullGraphInstanceStore(),
    )
    spec_id = spec_store.save(
        GraphSpec(
            name="empty",
            nodes=[],
            edges=[EdgeSpec(source=GraphNode.START, target=GraphNode.END)],
            state_class="default",
        )
    )

    await orchestrator.create_and_run(spec_id)

    assert orchestrator._active_instances == {}
