from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_graph import (
    CompiledGraph,
    DefaultGraphState,
    EdgeSpec,
    GraphContext,
    GraphEngine,
    GraphInstanceStatus,
    GraphNode,
    GraphPayload,
    GraphRuntime,
    GraphSpec,
    GraphSpecCompiler,
    NodeRegistry,
    SchedulerKind,
    create_null_coordinator,
)
from modex_graph.scheduler.bootstrap import BootstrapMode


class _TrackingRuntime(GraphRuntime):
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def before_node(
        self,
        ctx: GraphContext[DefaultGraphState],
        node_name: str,
    ) -> None:
        self.executed.append(node_name)


def _compile_empty(scheduler: SchedulerKind) -> CompiledGraph[DefaultGraphState]:
    registry = NodeRegistry()
    compiler = GraphSpecCompiler(registry, {"default": DefaultGraphState})
    return compiler.compile(
        GraphSpec(
            name="empty",
            nodes=[],
            edges=[EdgeSpec(source=GraphNode.START, target=GraphNode.END)],
            state_class="default",
            scheduler=scheduler,
        )
    )


class TestGraphPayload:
    def test_is_frozen_and_forbids_extra_fields(self) -> None:
        payload = GraphPayload(content="hello")

        with pytest.raises(ValidationError):
            payload.__setattr__("content", "changed")
        with pytest.raises(ValidationError):
            GraphPayload.model_validate({"content": "hello", "extra_field": "x"})

    def test_requires_string_content(self) -> None:
        with pytest.raises(ValidationError):
            GraphPayload.model_validate({"content": 123})


class TestDefaultGraphState:
    def test_result_is_mutable_payload_list(self) -> None:
        state = DefaultGraphState()
        result = [GraphPayload(content="done")]

        state.result = result

        assert state.result == result


class TestExecutableSentinels:
    @pytest.mark.parametrize("scheduler", list(SchedulerKind))
    async def test_empty_graph_executes_start_then_end(self, scheduler: SchedulerKind) -> None:
        compiled = _compile_empty(scheduler)
        runtime = _TrackingRuntime()
        coordinator = create_null_coordinator(1)
        for node in compiled.nodes.values():
            coordinator.register_node(node.node_id)

        state = DefaultGraphState()
        user_input = GraphPayload(content="hello")
        await GraphEngine(compiled).run_async(
            GraphContext(
                state=state,
                runtime=runtime,
                coordinator=coordinator,
                user_input=user_input,
            ),
            mode=BootstrapMode.FRESH
        )

        assert runtime.executed == [GraphNode.START, GraphNode.END]
        assert state.result == [user_input]

    def test_pending_is_a_graph_instance_status(self) -> None:
        assert GraphInstanceStatus.PENDING == "pending"
