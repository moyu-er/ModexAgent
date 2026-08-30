"""Turn-context descriptors and configuration pipeline.

Layered configuration across 3 orthogonal dimensions:

    Implementation: native (ReAct) / external (CLI)
    Topology:       normal (main) / subagent
    Mode:           session / graph

Graph mode is the upper layer — it sits above normal/subagent. Subagents
dispatched from within a graph turn remain atomic agents: they carry
``graph_instance_id`` (for reply routing) but never receive graph-exclusive
tools/hooks/providers. A subagent referenced DIRECTLY as a graph node,
however, is executing a graph node turn — its session binding carries
``is_node_execution`` and the configurators below fire for it like any
graph node (SPEC §4 axis 3: the signal is "does this turn's message carry
graph metadata", never the agent's comm kind).

Configurator gate matrix (native only; external skips configurator pipeline):

| Configurator        | Gate                                      | Fires on              |
|---------------------|-------------------------------------------|-----------------------|
| GraphContextBinding | graph_instance_id is not None             | All graph turns       |
| GraphApproval       | graph_instance_id is not None             | All graph turns       |
| GraphMaxTurns       | is_node_execution                         | Graph node turns      |
| GraphTool           | is_node_execution                         | Graph node turns      |
| GraphTopology       | is_node_execution                         | Graph node turns      |
| GraphKnowledge      | is_node_execution                         | Graph node turns      |

``is_node_execution`` comes from the session binding store (set by
``BotAgentNode.execute``), i.e. the graph scheduling signal itself — a
subagent session dispatched from a graph node never carries it, so such
subagents stay atomic graph-wise.

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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.tool_manager import Tool
from modex_agent.multi_agent.tools import SEND_TO_PEER_TOOL_NAME
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.tools.graph_tool_preset import GraphToolPreset
from modex_graph.context import GraphContext

if TYPE_CHECKING:
    from modex_agent.multi_agent.session_tree.session_binding import (
        SessionBindingStore,
    )
    from modex_agent.pipeline.turn_context_builder import TurnContextBuilder


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
    # Whether the current node has at least one AgentNode downstream.
    # Computed by BotAgentNode._build_graph_artifacts from graph topology.
    # Read by GraphWorkflowProvider to conditionally render Producer/Relay patterns.
    downstream_has_agent: bool = False
    # Whether the current node has __end__ as a direct downstream target.
    # Computed by BotAgentNode._build_graph_artifacts from graph topology.
    # Read by GraphWorkflowProvider to conditionally render the Final Reply pattern.
    downstream_has_end: bool = False


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


def wire_graph_turn_config(
    builder: TurnContextBuilder | None,
    *,
    graph_context_resolver: Callable[[int], GraphContext[Any] | None] | None,
    session_binding_store: SessionBindingStore | None,
) -> None:
    """Wire the graph turn-configuration trio onto a turn-context builder.

    Shared convergence point (architecture rule 15): the main pipeline
    (``_wire_main_pipeline`` in bot business code) AND the subagent
    materialization path (``AgentTemplate.materialize``) call this one
    function, so every agent that owns a turn lifecycle — main or
    lazily-materialized subagent — gets the same graph-mode per-turn
    configuration (binding store + context resolver + the 6
    configurators). Without it, a lazy subagent referenced by a graph node
    could run its graph turn but never receive the ``deliver`` tool
    (ticket 12).

    No-op when ``builder`` is ``None`` (external agents) or when no
    ``graph_context_resolver`` is wired (framework tests / graph-less
    deployments) — mirroring the main-pipeline guard shape.
    """
    if builder is None or graph_context_resolver is None:
        return
    builder.graph_context_resolver = graph_context_resolver
    if session_binding_store is not None:
        builder.session_binding_store = session_binding_store
    builder.config_pipeline = TurnContextConfigPipeline(
        [
            GraphContextBindingConfigurator(),
            GraphApprovalConfigurator(),
            GraphMaxTurnsConfigurator(),
            GraphToolConfigurator(),
            GraphTopologyConfigurator(),
            GraphKnowledgeConfigurator(),
        ]
    )


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
        return desc.is_node_execution

    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None:
        if ctx.runtime is None:
            return
        ctx.runtime.state.custom[TurnCustomKey.MAX_TURNS] = 3


class GraphToolConfigurator(TurnContextConfigurator):
    """Install graph-scoped tools (deliver + knowledge) on the tool manager."""

    def applies(self, desc: TurnContextDescriptor) -> bool:
        return desc.is_node_execution

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
        preset = GraphToolPreset(
            graph_tools,
            excluded_base_tools={SEND_TO_PEER_TOOL_NAME},
        )
        ctx.tool_manager = preset.build_tool_manager(ctx.tool_manager)


class GraphTopologyConfigurator(TurnContextConfigurator):
    """Publish graph topology + node description into turn state.

    The system prompt builder reads GRAPH_TOPOLOGY_CONTEXT and
    GRAPH_NODE_DESCRIPTION from turn state to render the
    "## Graph Node Context" section.
    """

    def applies(self, desc: TurnContextDescriptor) -> bool:
        return desc.is_node_execution

    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None:
        if ctx.runtime is None or desc.graph_artifacts is None:
            return
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = (
            desc.graph_artifacts.topology_section
        )
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_NODE_DESCRIPTION] = (
            desc.graph_artifacts.node_description
        )
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_AGENT] = (
            desc.graph_artifacts.downstream_has_agent
        )
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_DOWNSTREAM_HAS_END] = (
            desc.graph_artifacts.downstream_has_end
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
        return desc.is_node_execution

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
