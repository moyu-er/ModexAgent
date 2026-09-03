# ruff: noqa: ANN401
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import Tool
from modex_agent.memory.history import ListMessageHistory
from modex_agent.pipeline.turn_context_config import (
    GraphApprovalConfigurator,
    GraphContextBindingConfigurator,
    GraphKnowledgeConfigurator,
    GraphMaxTurnsConfigurator,
    GraphToolConfigurator,
    GraphTopologyConfigurator,
    GraphTurnArtifacts,
    TurnContextConfigPipeline,
    TurnContextConfigurator,
    TurnContextDescriptor,
)
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity, TurnStateBase
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager
from modex_graph.context import GraphContext


class RecordingConfigurator(TurnContextConfigurator):
    def __init__(self, name: str, applies: bool, calls: list[str]) -> None:
        self._name = name
        self._applies = applies
        self._calls = calls

    def applies(self, desc: TurnContextDescriptor) -> bool:
        self._calls.append(f"applies:{self._name}")
        return self._applies

    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None:
        self._calls.append(f"configure:{self._name}")


class _StubTool(Tool):
    def __init__(self, name: str = "stub") -> None:
        super().__init__(name=name, description="stub", parameters={})

    async def execute(self, **kwargs: Any) -> Any:
        return None


def make_context() -> AgentContext:
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
    )


def make_runtime_context() -> AgentContext:
    ctx = make_context()
    state = TurnStateBase(
        identity=TurnIdentity(
            agent_id="bot",
            session=SessionInfo.from_str("test.agent"),
            turn_id="t1",
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    ctx.runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return ctx


def make_descriptor() -> TurnContextDescriptor:
    return TurnContextDescriptor(
        agent_kind=AgentCommKind.NORMAL,
        execution_strategy=ExecutionStrategyKind.REACT,
    )


def make_graph_descriptor(
    *,
    agent_kind: AgentCommKind = AgentCommKind.NORMAL,
    is_node_execution: bool = True,
    graph_instance_id: int | None = 1,
    artifacts: GraphTurnArtifacts | None = None,
    graph_context: GraphContext[Any] | None = None,
) -> TurnContextDescriptor:
    return TurnContextDescriptor(
        agent_kind=agent_kind,
        execution_strategy=ExecutionStrategyKind.REACT,
        graph_instance_id=graph_instance_id,
        is_node_execution=is_node_execution,
        graph_artifacts=artifacts,
        graph_context=graph_context,
    )


def make_artifacts(
    *,
    deliver_tool: Tool | None = None,
    knowledge_config: Any = None,
    knowledge_dir: Path | None = None,
    topology_section: str = "### Topology\n- node-a",
    node_description: str = "researcher node",
    downstream_has_agent: bool = False,
    downstream_has_end: bool = False,
) -> GraphTurnArtifacts:
    return GraphTurnArtifacts(
        deliver_tool=deliver_tool or _StubTool(name="deliver"),
        topology_section=topology_section,
        node_description=node_description,
        knowledge_config=knowledge_config,
        knowledge_dir=knowledge_dir,
        downstream_has_agent=downstream_has_agent,
        downstream_has_end=downstream_has_end,
    )


def test_configure_short_circuits_when_descriptor_is_none() -> None:
    # Given
    calls: list[str] = []
    pipeline = TurnContextConfigPipeline(
        [RecordingConfigurator("first", True, calls)]
    )

    # When
    pipeline.configure(make_context(), None)

    # Then
    assert calls == []


def test_configure_runs_applicable_configurators_in_registration_order() -> None:
    # Given
    calls: list[str] = []
    pipeline = TurnContextConfigPipeline(
        [
            RecordingConfigurator("first", True, calls),
            RecordingConfigurator("second", True, calls),
        ]
    )

    # When
    pipeline.configure(make_context(), make_descriptor())

    # Then
    assert calls == [
        "applies:first",
        "configure:first",
        "applies:second",
        "configure:second",
    ]


def test_configure_skips_configurator_when_applies_is_false() -> None:
    # Given
    calls: list[str] = []
    pipeline = TurnContextConfigPipeline(
        [
            RecordingConfigurator("skipped", False, calls),
            RecordingConfigurator("applied", True, calls),
        ]
    )

    # When
    pipeline.configure(make_context(), make_descriptor())

    # Then
    assert calls == [
        "applies:skipped",
        "applies:applied",
        "configure:applied",
    ]


def test_descriptor_rejects_mutation() -> None:
    # Given
    descriptor = make_descriptor()

    # When / Then
    with pytest.raises(ValidationError):
        descriptor.graph_node_name = "changed"


def test_agent_context_graph_instance_id_defaults_to_none() -> None:
    # Given / When
    context = make_context()

    # Then
    assert context.graph_instance_id is None


def test_binding_applies_when_graph_instance_id_present() -> None:
    # Given
    configurator = GraphContextBindingConfigurator()
    desc = make_graph_descriptor(graph_instance_id=42)

    # When / Then
    assert configurator.applies(desc) is True


def test_binding_does_not_apply_when_graph_instance_id_none() -> None:
    # Given
    configurator = GraphContextBindingConfigurator()
    desc = make_graph_descriptor(graph_instance_id=None)

    # When / Then
    assert configurator.applies(desc) is False


def test_binding_sets_graph_instance_id_and_context() -> None:
    # Given
    configurator = GraphContextBindingConfigurator()
    graph_ctx = MagicMock(spec=GraphContext)
    desc = make_graph_descriptor(graph_instance_id=7, graph_context=graph_ctx)
    ctx = make_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.graph_instance_id == 7
    assert ctx.graph_context is graph_ctx


def test_binding_leaves_graph_context_untouched_when_descriptor_has_none() -> None:
    # Given
    configurator = GraphContextBindingConfigurator()
    desc = make_graph_descriptor(graph_instance_id=7, graph_context=None)
    ctx = make_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.graph_instance_id == 7
    assert ctx.graph_context is None


def test_approval_applies_when_graph_instance_id_present() -> None:
    # Given
    configurator = GraphApprovalConfigurator()
    desc = make_graph_descriptor(graph_instance_id=1)

    # When / Then
    assert configurator.applies(desc) is True


def test_approval_does_not_apply_when_graph_instance_id_none() -> None:
    # Given
    configurator = GraphApprovalConfigurator()
    desc = make_graph_descriptor(graph_instance_id=None)

    # When / Then
    assert configurator.applies(desc) is False


def test_approval_clears_runtime_approval_when_runtime_present() -> None:
    # Given
    configurator = GraphApprovalConfigurator()
    desc = make_graph_descriptor(graph_instance_id=1)
    ctx = make_runtime_context()
    sentinel_approval = object()
    ctx.runtime.services.approval = sentinel_approval  # type: ignore[assignment]

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.runtime.services.approval is None


def test_approval_skips_when_runtime_none() -> None:
    # Given
    configurator = GraphApprovalConfigurator()
    desc = make_graph_descriptor(graph_instance_id=1)
    ctx = make_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.runtime is None


def test_max_turns_applies_for_normal_node_execution() -> None:
    # Given
    configurator = GraphMaxTurnsConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.NORMAL, is_node_execution=True
    )

    # When / Then
    assert configurator.applies(desc) is True


def test_max_turns_applies_for_subagent_node_execution() -> None:
    # Given — a lazy subagent leaf referenced directly as a graph node
    # (SPEC §4 axis 3): the gate is the graph scheduling signal carried by
    # the session binding, never the agent's comm kind.
    configurator = GraphMaxTurnsConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.SUBAGENT, is_node_execution=True
    )

    # When / Then
    assert configurator.applies(desc) is True


def test_max_turns_does_not_apply_for_subagent_dispatched_from_graph() -> None:
    # Given — a subagent dispatched from within a graph turn: its session
    # binding carries no node-execution signal, so it stays atomic.
    configurator = GraphMaxTurnsConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.SUBAGENT, is_node_execution=False
    )

    # When / Then
    assert configurator.applies(desc) is False


def test_max_turns_does_not_apply_when_not_node_execution() -> None:
    # Given
    configurator = GraphMaxTurnsConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.NORMAL, is_node_execution=False
    )

    # When / Then
    assert configurator.applies(desc) is False


def test_max_turns_sets_custom_max_turns_to_3() -> None:
    # Given
    configurator = GraphMaxTurnsConfigurator()
    desc = make_graph_descriptor()
    ctx = make_runtime_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.runtime.state.custom[TurnCustomKey.MAX_TURNS] == 3


def test_max_turns_skips_when_runtime_none() -> None:
    # Given
    configurator = GraphMaxTurnsConfigurator()
    desc = make_graph_descriptor()
    ctx = make_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.runtime is None


def test_tool_applies_for_normal_node_execution() -> None:
    # Given
    configurator = GraphToolConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.NORMAL, is_node_execution=True
    )

    # When / Then
    assert configurator.applies(desc) is True


def test_tool_applies_for_subagent_node_execution() -> None:
    # Given — a lazy subagent leaf referenced directly as a graph node
    # (SPEC §4 axis 3): the gate is the graph scheduling signal carried by
    # the session binding, never the agent's comm kind.
    configurator = GraphToolConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.SUBAGENT, is_node_execution=True
    )

    # When / Then
    assert configurator.applies(desc) is True


def test_tool_does_not_apply_for_subagent_dispatched_from_graph() -> None:
    # Given — a subagent dispatched from within a graph turn: its session
    # binding carries no node-execution signal, so it stays atomic.
    configurator = GraphToolConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.SUBAGENT, is_node_execution=False
    )

    # When / Then
    assert configurator.applies(desc) is False


def test_tool_replaces_tool_manager_with_preset() -> None:
    # Given
    configurator = GraphToolConfigurator()
    deliver = _StubTool(name="deliver")
    artifacts = make_artifacts(deliver_tool=deliver)
    desc = make_graph_descriptor(artifacts=artifacts)
    ctx = make_context()
    original_manager = ctx.tool_manager

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.tool_manager is not original_manager
    assert "deliver" in ctx.tool_manager.list_tools()


def test_tool_skips_when_graph_artifacts_none() -> None:
    # Given
    configurator = GraphToolConfigurator()
    desc = make_graph_descriptor(artifacts=None)
    ctx = make_context()
    original_manager = ctx.tool_manager

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.tool_manager is original_manager


def test_tool_installs_knowledge_tool_when_dir_set() -> None:
    # Given
    configurator = GraphToolConfigurator()
    deliver = _StubTool(name="deliver")
    tmp_dir = Path(tempfile.mkdtemp())
    artifacts = make_artifacts(deliver_tool=deliver, knowledge_dir=tmp_dir)
    desc = make_graph_descriptor(artifacts=artifacts)
    ctx = make_context()

    # When
    configurator.configure(ctx, desc)

    # Then — both deliver and knowledge_base tools installed
    assert "deliver" in ctx.tool_manager.list_tools()
    assert "knowledge_base" in ctx.tool_manager.list_tools()


def test_tool_skips_knowledge_when_dir_none() -> None:
    # Given
    configurator = GraphToolConfigurator()
    deliver = _StubTool(name="deliver")
    artifacts = make_artifacts(deliver_tool=deliver, knowledge_dir=None)
    desc = make_graph_descriptor(artifacts=artifacts)
    ctx = make_context()

    # When
    configurator.configure(ctx, desc)

    # Then — only deliver installed, no knowledge_base
    assert "deliver" in ctx.tool_manager.list_tools()
    assert "knowledge_base" not in ctx.tool_manager.list_tools()


def test_topology_applies_for_normal_node_execution() -> None:
    # Given
    configurator = GraphTopologyConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.NORMAL, is_node_execution=True
    )

    # When / Then
    assert configurator.applies(desc) is True


def test_topology_applies_for_subagent_node_execution() -> None:
    # Given — a lazy subagent leaf referenced directly as a graph node
    # (SPEC §4 axis 3): the gate is the graph scheduling signal carried by
    # the session binding, never the agent's comm kind.
    configurator = GraphTopologyConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.SUBAGENT, is_node_execution=True
    )

    # When / Then
    assert configurator.applies(desc) is True


def test_topology_does_not_apply_for_subagent_dispatched_from_graph() -> None:
    # Given — a subagent dispatched from within a graph turn: its session
    # binding carries no node-execution signal, so it stays atomic.
    configurator = GraphTopologyConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.SUBAGENT, is_node_execution=False
    )

    # When / Then
    assert configurator.applies(desc) is False


def test_topology_publishes_topology_and_node_description() -> None:
    # Given
    configurator = GraphTopologyConfigurator()
    artifacts = make_artifacts(
        topology_section="### Topology\n- a -> b",
        node_description="researcher node",
    )
    desc = make_graph_descriptor(artifacts=artifacts)
    ctx = make_runtime_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert (
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT]
        == "### Topology\n- a -> b"
    )
    assert (
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_NODE_DESCRIPTION]
        == "researcher node"
    )


def test_topology_publishes_downstream_type_flags() -> None:
    # Given
    configurator = GraphTopologyConfigurator()
    artifacts = make_artifacts(downstream_has_agent=True, downstream_has_end=True)
    desc = make_graph_descriptor(artifacts=artifacts)
    ctx = make_runtime_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.runtime.state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_AGENT] is True
    assert ctx.runtime.state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_END] is True


def test_topology_skips_when_runtime_none() -> None:
    # Given
    configurator = GraphTopologyConfigurator()
    artifacts = make_artifacts()
    desc = make_graph_descriptor(artifacts=artifacts)
    ctx = make_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.runtime is None


def test_topology_skips_when_graph_artifacts_none() -> None:
    # Given
    configurator = GraphTopologyConfigurator()
    desc = make_graph_descriptor(artifacts=None)
    ctx = make_runtime_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT not in ctx.runtime.state.custom
    assert TurnCustomKey.GRAPH_NODE_DESCRIPTION not in ctx.runtime.state.custom


def test_knowledge_applies_for_normal_node_execution() -> None:
    # Given
    configurator = GraphKnowledgeConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.NORMAL, is_node_execution=True
    )

    # When / Then
    assert configurator.applies(desc) is True


def test_knowledge_applies_for_subagent_node_execution() -> None:
    # Given — a lazy subagent leaf referenced directly as a graph node
    # (SPEC §4 axis 3): the gate is the graph scheduling signal carried by
    # the session binding, never the agent's comm kind.
    configurator = GraphKnowledgeConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.SUBAGENT, is_node_execution=True
    )

    # When / Then
    assert configurator.applies(desc) is True


def test_knowledge_does_not_apply_for_subagent_dispatched_from_graph() -> None:
    # Given — a subagent dispatched from within a graph turn: its session
    # binding carries no node-execution signal, so it stays atomic.
    configurator = GraphKnowledgeConfigurator()
    desc = make_graph_descriptor(
        agent_kind=AgentCommKind.SUBAGENT, is_node_execution=False
    )

    # When / Then
    assert configurator.applies(desc) is False


def test_knowledge_publishes_knowledge_dir_and_requirements(tmp_path: Path) -> None:
    # Given
    configurator = GraphKnowledgeConfigurator()

    class _Config:
        require_read = True
        require_write = False

    knowledge_dir = tmp_path / "knowledge"
    artifacts = make_artifacts(
        knowledge_config=_Config(),
        knowledge_dir=knowledge_dir,
    )
    desc = make_graph_descriptor(artifacts=artifacts)
    ctx = make_runtime_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_DIR] == str(
        knowledge_dir
    )
    assert (
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ]
        is True
    )
    assert (
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE]
        is False
    )


def test_knowledge_skips_when_runtime_none() -> None:
    # Given
    configurator = GraphKnowledgeConfigurator()
    artifacts = make_artifacts()
    desc = make_graph_descriptor(artifacts=artifacts)
    ctx = make_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.runtime is None


def test_knowledge_skips_when_graph_artifacts_none() -> None:
    # Given
    configurator = GraphKnowledgeConfigurator()
    desc = make_graph_descriptor(artifacts=None)
    ctx = make_runtime_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert TurnCustomKey.GRAPH_KNOWLEDGE_DIR not in ctx.runtime.state.custom
    assert TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ not in ctx.runtime.state.custom
    assert TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE not in ctx.runtime.state.custom


def test_knowledge_defaults_requirements_to_false_when_config_missing() -> None:
    # Given
    configurator = GraphKnowledgeConfigurator()
    artifacts = make_artifacts(knowledge_config=None, knowledge_dir=None)
    desc = make_graph_descriptor(artifacts=artifacts)
    ctx = make_runtime_context()

    # When
    configurator.configure(ctx, desc)

    # Then
    assert ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_DIR] is None
    assert (
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ]
        is False
    )
    assert (
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE]
        is False
    )
