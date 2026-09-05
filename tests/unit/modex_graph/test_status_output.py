from helpers import make_coordinator

from modex_graph import GraphInstanceStatus, GraphOutput, GraphOutputAdapter, GraphOutputKind


async def test_status_change_round_trips_through_coordinator_output() -> None:
    class RecordingAdapter(GraphOutputAdapter):
        def __init__(self) -> None:
            self.outputs: list[GraphOutput] = []

        async def emit(self, output: GraphOutput) -> None:
            self.outputs.append(GraphOutput.model_validate_json(output.model_dump_json()))

    adapter = RecordingAdapter()
    coordinator = make_coordinator()
    coordinator.set_output_adapter(adapter)
    coordinator.emit_output(
        GraphOutputKind.STATUS_CHANGED, status=GraphInstanceStatus.PAUSING,
    )
    await coordinator.drain_output_events()
    assert len(adapter.outputs) == 1
    output = adapter.outputs[0]
    assert output.kind.value == "graph_status_changed"
    assert output.status is GraphInstanceStatus.PAUSING
    assert output.graph_instance_id == coordinator.graph_instance_id
    assert output.timestamp is not None
