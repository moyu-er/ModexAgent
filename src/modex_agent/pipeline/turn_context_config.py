"""Turn-context descriptors and configuration pipeline.

Layered configuration across 3 orthogonal dimensions:

    Implementation: native (ReAct) / external (CLI)
    Topology:       normal (main) / subagent
    Mode:           session / graph

Graph mode is the upper layer — it sits above normal/subagent. Subagents in
graph mode remain atomic agents: they carry ``graph_instance_id`` (for reply
routing) but never receive graph-exclusive tools/hooks/providers.

Configurator gate matrix (native only; external skips configurator pipeline):

| Configurator        | Gate                                      | Fires on              |
|---------------------|-------------------------------------------|-----------------------|
| GraphContextBinding | graph_instance_id is not None             | All graph turns       |
| GraphApproval       | graph_instance_id is not None             | All graph turns       |
| GraphMaxTurns       | is_node_execution and NORMAL              | Graph node main only  |
| GraphTool           | is_node_execution and NORMAL              | Graph node main only  |
| GraphTopology       | is_node_execution and NORMAL              | Graph node main only  |
| GraphKnowledge      | is_node_execution and NORMAL              | Graph node main only  |

Examples of graph-exclusive components that fire only on graph node main
agents: ``GraphWorkflowProvider`` (system prompt), ``KnowledgeHook``
(before/after turn), ``DeliverRetryHook`` (after turn), ``GraphDeliverTool``,
``GraphKnowledgeBaseTool``. Each has its own runtime gate (e.g. checking
``GRAPH_TOPOLOGY_CONTEXT`` state key or deliver tool existence) to exclude
subagents even when ``graph_context`` is set.

Peer communication: in graph mode, ``CommunicationTargetStore`` filters out
NORMAL (peer) targets — the agent cannot perceive or reach peers. Graph nodes
communicate via ``deliver`` (graph edges). In session mode, peer sends go to
the receiver's tree via ``target.tree_ref`` (cross-tree), and
``should_propagate_graph_instance_id() → False`` ensures no graph
contamination.

See ``docs/design/session-tree/layered-config-matrix.md`` for the full design.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.tool_manager import Tool
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.tools.graph_tool_preset import GraphToolPreset
from modex_graph.context import GraphContext


class GraphTurnArtifacts(BaseModel):
    """Pre-built graph artifacts needed to configure one agent turn."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    deliver_tool: Tool
    topology_section: str
    node_description: str
    knowledge_config: Any
    knowledge_dir: Path | None = None


class TurnContextDescriptor(BaseModel):
    """Typed inputs that determine per-turn AgentContext configuration."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    agent_kind: AgentCommKind
    execution_strategy: ExecutionStrategyKind
    graph_context: GraphContext[Any] | None = None
    graph_node_name: str | None = None
    graph_instance_id: int | None = None
    is_node_execution: bool = False
    graph_artifacts: GraphTurnArtifacts | None = None


class TurnContextConfigurator(ABC):
    """Extension point for synchronous per-turn context configuration."""

    @abstractmethod
    def applies(self, desc: TurnContextDescriptor) -> bool:
        """Return whether this configurator applies to the descriptor."""
        ...

    @abstractmethod
    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None:
        """Apply this configurator to the agent context."""
        ...


class TurnContextConfigPipeline:
    """Apply matching configurators in registration order."""

    def __init__(self, configurators: list[TurnContextConfigurator]) -> None:
        self._configurators = configurators

    def configure(
        self,
        ctx: AgentContext,
        desc: TurnContextDescriptor | None,
    ) -> None:
        if desc is None:
            return
        for configurator in self._configurators:
            if configurator.applies(desc):
                configurator.configure(ctx, desc)


class GraphContextBindingConfigurator(TurnContextConfigurator):
    """Bind graph instance id and context onto AgentContext for graph turns."""

    def applies(self, desc: TurnContextDescriptor) -> bool:
        return desc.graph_instance_id is not None

    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None:
        ctx.graph_instance_id = desc.graph_instance_id
        if desc.graph_context is not None:
            ctx.graph_context = desc.graph_context


class GraphApprovalConfigurator(TurnContextConfigurator):
    """Disable per-turn approval for graph-context turns.

    Graph nodes run inside a pre-defined workflow — tools execute as designed
    intent, not user-interacted agent actions. Approval suspension would
    deadlock the graph (no user to approve, no way to resume). Per-turn
    override on AgentRuntimeServices; pool-level base services are unaffected.
    """

    def applies(self, desc: TurnContextDescriptor) -> bool:
        return desc.graph_instance_id is not None

    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None:
        if ctx.runtime is None:
            return
        ctx.runtime.services.approval = None


class GraphMaxTurnsConfigurator(TurnContextConfigurator):
    """Cap ReAct iterations for graph node execution at 3.

    Graph nodes are scoped tasks — capping prevents a runaway loop from
    burning tokens inside a workflow the designer expects to terminate.
    """

    def applies(self, desc: TurnContextDescriptor) -> bool:
        return desc.is_node_execution and desc.agent_kind == AgentCommKind.NORMAL

    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None:
        if ctx.runtime is None:
            return
        ctx.runtime.state.custom[TurnCustomKey.MAX_TURNS] = 3


class GraphToolConfigurator(TurnContextConfigurator):
    """Install graph-scoped tools (deliver + knowledge) on the tool manager."""

    def applies(self, desc: TurnContextDescriptor) -> bool:
        return desc.is_node_execution and desc.agent_kind == AgentCommKind.NORMAL

    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None:
        if desc.graph_artifacts is None:
            return
        artifacts = desc.graph_artifacts
        graph_tools: list[Tool] = [artifacts.deliver_tool]
        # Build and install knowledge tool when knowledge dir is resolved.
        # The tool is constructed here (not in BotAgentNode._build_graph_artifacts)
        # because it needs ctx.tool_manager for capability detection — which is
        # only available after build_runtime_and_context constructs AgentContext.
        if artifacts.knowledge_dir is not None:
            from modex_agent.tools.graph_knowledge_capabilities import (
                KnowledgeToolCapabilities,
            )
            from modex_agent.tools.graph_knowledge_tool import GraphKnowledgeBaseTool

            capabilities = KnowledgeToolCapabilities.from_tool_manager(ctx.tool_manager)
            knowledge_tool = GraphKnowledgeBaseTool(
                knowledge_dir=artifacts.knowledge_dir,
                capabilities=capabilities,
                node_name=desc.graph_node_name or "",
            )
            graph_tools.append(knowledge_tool)
        preset = GraphToolPreset(graph_tools)
        ctx.tool_manager = preset.build_tool_manager(ctx.tool_manager)


class GraphTopologyConfigurator(TurnContextConfigurator):
    """Publish graph topology + node description into turn state.

    The system prompt builder reads GRAPH_TOPOLOGY_CONTEXT and
    GRAPH_NODE_DESCRIPTION from turn state to render the
    "## Graph Node Context" section.
    """

    def applies(self, desc: TurnContextDescriptor) -> bool:
        return desc.is_node_execution and desc.agent_kind == AgentCommKind.NORMAL

    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None:
        if ctx.runtime is None or desc.graph_artifacts is None:
            return
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = (
            desc.graph_artifacts.topology_section
        )
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_NODE_DESCRIPTION] = (
            desc.graph_artifacts.node_description
        )


class GraphKnowledgeConfigurator(TurnContextConfigurator):
    """Publish knowledge directory + read/write requirements into turn state.

    KnowledgeHook reads these keys to inject findings/open_questions summaries
    at turn start and to enforce per-node knowledge requirements.
    ``knowledge_config`` is typed as ``Any`` in GraphTurnArtifacts because it
    is a real extension boundary — business code may carry arbitrary config
    objects. ``getattr(..., 'require_read', False)`` is the contracted
    fallback per type-safety rule 6.
    """

    def applies(self, desc: TurnContextDescriptor) -> bool:
        return desc.is_node_execution and desc.agent_kind == AgentCommKind.NORMAL

    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None:
        if ctx.runtime is None or desc.graph_artifacts is None:
            return
        artifacts = desc.graph_artifacts
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_DIR] = (
            str(artifacts.knowledge_dir) if artifacts.knowledge_dir else None
        )
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = (
            getattr(artifacts.knowledge_config, "require_read", False)
        )
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE] = (
            getattr(artifacts.knowledge_config, "require_write", False)
        )
