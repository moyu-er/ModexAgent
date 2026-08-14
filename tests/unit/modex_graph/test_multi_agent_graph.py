from __future__ import annotations

from typing import Final

from helpers import TrackingRuntime, make_coordinator, make_ctx, register_graph_nodes

from modex_agent.agents.agent_node import AgentNode
from modex_agent.core.session_registry import InMemorySessionRegistry, SessionRegistry
from modex_graph import (
    DefaultGraphState,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphPayload,
    IntegratedInput,
    NodeTrigger,
    SchedulerKind,
)

RESEARCHER: Final = "researcher"
ANALYZER: Final = "analyzer"
REVIEWER: Final = "reviewer"
DOCUMENTER: Final = "documenter"
SYNTHESIZER: Final = "synthesizer"


class _ResearchState(DefaultGraphState):
    research_data: str = ""
    analysis: str = ""
    review: str = ""
    documentation: str = ""
    final_result: str = ""
    reviewer_input: str = ""
    documenter_input: str = ""
    execution_order: list[str] = []
    execution_count: int = 0
    synthesizer_sources: list[str] = []
    synthesizer_payloads: list[str] = []
    upstream_status: str = ""


def _payload_texts(integrated_input: IntegratedInput) -> list[str]:
    return [payload.content.content for payload in integrated_input.payloads]


class _ResearchAgentNode(AgentNode):
    def __init__(self, registry: SessionRegistry) -> None:
        super().__init__()
        self._registry = registry

    def agent_name(self) -> str:
        return self.name

    async def _resolve_session_registry(self) -> SessionRegistry:
        return self._registry

    def _record_execution(self, ctx: GraphContext[_ResearchState]) -> None:
        ctx.state.execution_order.append(self.name)
        ctx.state.execution_count += 1


class _ResearcherNode(_ResearchAgentNode):
    async def execute(
        self, ctx: GraphContext[_ResearchState], integrated_input: IntegratedInput
    ) -> None:
        request = _payload_texts(integrated_input)[0]
        ctx.state.research_data = f"research findings for {request}"
        self._record_execution(ctx)
        self.deliver(GraphPayload(content=ctx.state.research_data), ANALYZER, ctx)


class _AnalyzerNode(_ResearchAgentNode):
    def __init__(self, registry: SessionRegistry, *, include_documenter: bool) -> None:
        super().__init__(registry)
        self._include_documenter = include_documenter

    async def execute(
        self, ctx: GraphContext[_ResearchState], integrated_input: IntegratedInput
    ) -> None:
        research_data = _payload_texts(integrated_input)[0]
        ctx.state.analysis = f"analysis of {research_data}"
        self._record_execution(ctx)
        payload = GraphPayload(content=ctx.state.analysis)
        self.deliver(payload, REVIEWER, ctx)
        if self._include_documenter:
            self.deliver(payload, DOCUMENTER, ctx)


class _ReviewerNode(_ResearchAgentNode):
    async def execute(
        self, ctx: GraphContext[_ResearchState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.reviewer_input = _payload_texts(integrated_input)[0]
        ctx.state.review = f"review of {ctx.state.reviewer_input}"
        self._record_execution(ctx)
        self.deliver(GraphPayload(content=ctx.state.review), SYNTHESIZER, ctx)


class _DocumenterNode(_ResearchAgentNode):
    async def execute(
        self, ctx: GraphContext[_ResearchState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.documenter_input = _payload_texts(integrated_input)[0]
        ctx.state.documentation = f"documentation of {ctx.state.documenter_input}"
        self._record_execution(ctx)
        self.deliver(GraphPayload(content=ctx.state.documentation), SYNTHESIZER, ctx)


class _SynthesizerNode(_ResearchAgentNode):
    trigger = NodeTrigger.ON_ALL_PREDS

    def __init__(
        self,
        registry: SessionRegistry,
        reviewer: _ReviewerNode,
        documenter: _DocumenterNode,
    ) -> None:
        super().__init__(registry)
        self._upstreams = (reviewer, documenter)

    def _format_integrated_input(self, integrated_input: IntegratedInput) -> str:
        delivered = {payload.source_node for payload in integrated_input.payloads}
        return "\n".join(
            f"{node.name}: {'delivered' if node.node_id in delivered else 'missing'}"
            for node in self._upstreams
        )

    async def execute(
        self, ctx: GraphContext[_ResearchState], integrated_input: IntegratedInput
    ) -> None:
        contents_by_source = {
            payload.source_node: payload.content.content
            for payload in integrated_input.payloads
        }
        ctx.state.synthesizer_sources = [
            node.name for node in self._upstreams if node.node_id in contents_by_source
        ]
        ctx.state.synthesizer_payloads = [
            contents_by_source[node.node_id]
            for node in self._upstreams
            if node.node_id in contents_by_source
        ]
        ctx.state.upstream_status = self._format_integrated_input(integrated_input)
        ctx.state.final_result = "synthesis: " + " + ".join(
            ctx.state.synthesizer_payloads
        )
        self._record_execution(ctx)
        self.deliver(GraphPayload(content=ctx.state.final_result), GraphNode.END, ctx)


async def _run_scenario(
    *, include_documenter: bool = True
) -> tuple[_ResearchState, TrackingRuntime]:
    registry = InMemorySessionRegistry()
    researcher = _ResearcherNode(registry)
    analyzer = _AnalyzerNode(registry, include_documenter=include_documenter)
    reviewer = _ReviewerNode(registry)
    documenter = _DocumenterNode(registry)
    synthesizer = _SynthesizerNode(registry, reviewer, documenter)

    graph: Graph[_ResearchState] = Graph(name="multi-agent-research")
    graph.add_node(RESEARCHER, researcher)
    graph.add_node(ANALYZER, analyzer)
    graph.add_node(REVIEWER, reviewer)
    graph.add_node(DOCUMENTER, documenter)
    graph.add_node(SYNTHESIZER, synthesizer)
    graph.add_edge(GraphNode.START, RESEARCHER)
    graph.add_edge(RESEARCHER, ANALYZER)
    graph.add_edge(ANALYZER, REVIEWER)
    graph.add_edge(ANALYZER, DOCUMENTER)
    graph.add_edge(REVIEWER, SYNTHESIZER)
    graph.add_edge(DOCUMENTER, SYNTHESIZER)
    graph.add_edge(SYNTHESIZER, GraphNode.END)
    compiled = graph.compile(
        scheduler=SchedulerKind.PARALLEL,
        default_trigger=NodeTrigger.ON_ALL_PREDS,
    )

    coordinator = make_coordinator()
    helper_ctx = make_ctx(coordinator=coordinator)
    runtime = TrackingRuntime()
    state = _ResearchState()
    ctx = GraphContext(
        state=state,
        runtime=runtime,
        coordinator=helper_ctx.coordinator,
        user_input=GraphPayload(content="solid-state batteries"),
        scheduler_kind=SchedulerKind.PARALLEL,
    )
    register_graph_nodes(ctx.coordinator, compiled)

    result = await GraphEngine(compiled).run_async(ctx)
    assert result is state
    return state, runtime


class TestMultiAgentGraphFanOutFanIn:
    async def test_full_dag_executes_all_nodes_in_topological_order(self) -> None:
        state, runtime = await _run_scenario()

        positions = {name: state.execution_order.index(name) for name in state.execution_order}
        assert positions[RESEARCHER] < positions[ANALYZER]
        assert positions[ANALYZER] < positions[REVIEWER] < positions[SYNTHESIZER]
        assert positions[ANALYZER] < positions[DOCUMENTER] < positions[SYNTHESIZER]
        assert state.execution_count == 5
        assert runtime.before_calls == runtime.after_calls
        assert set(runtime.before_calls) == {
            GraphNode.START,
            RESEARCHER,
            ANALYZER,
            REVIEWER,
            DOCUMENTER,
            SYNTHESIZER,
            GraphNode.END,
        }

    async def test_analyzer_fan_out_reaches_both_downstream_agents(self) -> None:
        state, _ = await _run_scenario()

        assert state.research_data == "research findings for solid-state batteries"
        assert state.analysis == f"analysis of {state.research_data}"
        assert state.reviewer_input == state.analysis
        assert state.documenter_input == state.analysis
        assert state.review == f"review of {state.analysis}"
        assert state.documentation == f"documentation of {state.analysis}"

    async def test_synthesizer_integrates_both_upstream_payloads(self) -> None:
        state, _ = await _run_scenario()

        assert state.synthesizer_sources == [REVIEWER, DOCUMENTER]
        assert state.synthesizer_payloads == [state.review, state.documentation]
        assert state.upstream_status == "reviewer: delivered\ndocumenter: delivered"

    async def test_end_aggregates_synthesized_output_into_result(self) -> None:
        state, _ = await _run_scenario()

        assert state.final_result == f"synthesis: {state.review} + {state.documentation}"
        assert state.result == [GraphPayload(content=state.final_result)]


class TestConditionalRouting:
    async def test_analyzer_skips_documenter_but_synthesizer_still_runs(self) -> None:
        state, _ = await _run_scenario(include_documenter=False)

        assert DOCUMENTER not in state.execution_order
        assert state.documentation == ""
        assert state.execution_order[-1] == SYNTHESIZER
        assert state.execution_count == 4

    async def test_synthesizer_runs_with_only_reviewer_input(self) -> None:
        state, _ = await _run_scenario(include_documenter=False)

        assert state.synthesizer_sources == [REVIEWER]
        assert state.synthesizer_payloads == [state.review]
        assert state.final_result == f"synthesis: {state.review}"
        assert state.result == [GraphPayload(content=state.final_result)]


class TestUpstreamStatus:
    async def test_missing_upstream_is_shown_in_integrated_input_status(self) -> None:
        state, _ = await _run_scenario(include_documenter=False)

        assert state.upstream_status == "reviewer: delivered\ndocumenter: missing"
