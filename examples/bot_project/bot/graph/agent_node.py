"""BotAgentNode -- agent-backed graph node wiring pool/pipeline resources.

Bridges the static graph scheduling layer (modex_graph) to the bot's pool-mode
agent runtime. Each node binds a named agent from a named pool and drives a
graph turn via the session tree: pre-build graph artifacts → tree.deliver →
tree.wait_quiesce → return. The agent may be a never-dispatched lazy leaf —
instance absence is not an error: the InboxPoller cold-starts the agent from
its template (the same inbox-driven materialization as session mode, SPEC §4
axis 3), so this node never resolves the instance up front. Per-turn
configuration (tools, topology, approval, knowledge) is handled by the
TurnContextConfigPipeline configurators, not inline mutation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, assert_never

from bot.graph.knowledge_config import KnowledgeNodeConfig
from modex_agent.agents.agent_node import AgentNode, SessionStrategy
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.session_tree.session_binding import SessionBinding
from modex_agent.pipeline.turn_context_config import GraphTurnArtifacts
from modex_agent.tools.graph_deliver import GraphDeliverTargetStore, GraphDeliverTool
from modex_graph.constants import FrameworkPayloadSource, GraphNode

if TYPE_CHECKING:
    from bot.workspace.handle import WorkspaceResolverCell
    from modex_agent.core.session_registry import SessionRegistry
    from modex_agent.multi_agent.descriptor import AgentInstance
    from modex_agent.multi_agent.pool_instance import PoolInstance
    from modex_agent.multi_agent.template import AgentTemplate
    from modex_graph.context import GraphContext
    from modex_graph.integration import IntegratedInput


class BotAgentNode(AgentNode):
    """Graph node backed by a pool agent with full turn-context lifecycle."""

    def __init__(
        self,
        agent_name: str,
        pool_name: str,
        workspace_resolver: WorkspaceResolverCell,
        *,
        session_strategy: SessionStrategy = SessionStrategy.CACHED,
        knowledge_config: KnowledgeNodeConfig | None = None,
        node_description: str | None = None,
    ) -> None:
        super().__init__(session_strategy=session_strategy)
        self._agent_name = agent_name
        self._pool_name = pool_name
        self._workspace_resolver = workspace_resolver
        self._deliver_tool: GraphDeliverTool | None = None
        self._knowledge_config = knowledge_config or KnowledgeNodeConfig()
        self._description = node_description

    def agent_name(self) -> str:
        return self._agent_name

    async def _resolve_session_registry(self) -> SessionRegistry:
        pool = self._resolve_pool()
        registry: SessionRegistry | None = pool.pool.session_registry
        if registry is None:
            raise RuntimeError(f"Pool {self._pool_name!r} has no session registry")
        return registry

    async def _create_session(self, ctx: GraphContext[Any]) -> SessionInfo:
        from modex_graph.utils.id import generate_id as _generate_id
        agent_name = self.agent_name()
        match self._session_strategy:
            case SessionStrategy.CACHED:
                external_id = f"{self.node_id}.{agent_name}"
            case SessionStrategy.PER_INVOCATION:
                external_id = f"{self.node_id}.{agent_name}.{_generate_id()}"
            case unreachable:
                assert_never(unreachable)
        session = SessionIdFactory().create(
            agent_name,
            external_id=external_id,
            metadata={"pool": self._pool_name},
        )
        registry = await self._resolve_session_registry()
        await registry.register(session)
        return session

    def _resolve_pool(self) -> PoolInstance:
        workspace = self._workspace_resolver.resolve_workspace()
        pool = workspace.pools.get(self._pool_name)
        if pool is None:
            raise RuntimeError(f"Pool {self._pool_name!r} not found in workspace")
        return pool

    def resolve_description(self) -> str:
        if self._description:
            return self._description
        agent_pool = self._resolve_pool().pool
        instance: AgentInstance | None = agent_pool.get(self._agent_name)
        if instance is not None:
            return instance.descriptor.role_description or AgentNode.DESCRIPTION_NOT_FOUND
        # Never-dispatched lazy agent (fresh boot): the template registry is
        # the compiled declaration's runtime carrier — boot seeds it from the
        # 06 compilation and the InboxPoller materializes from the same
        # source, so existence and description resolve without an instance.
        # ``role_description`` and the template description carry the same
        # declared value on both boot roads, so the order never diverges.
        template: AgentTemplate | None = agent_pool.get_template(self._agent_name)
        if template is not None:
            return template.spec.description or AgentNode.DESCRIPTION_NOT_FOUND
        return AgentNode.DESCRIPTION_NOT_FOUND

    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> None:
        # 1. Ensure session.
        session = await self._ensure_session(ctx)

        binding_store = self._resolve_pool().session_binding_store
        tree = self._resolve_pool().tree_manager
        bound_here = False

        try:
            # 2. Build artifacts and store on graph context for the configurator pipeline.
            artifacts = self._build_graph_artifacts(ctx)
            if ctx.user_data is None:
                ctx.user_data = {}
            ctx.user_data.setdefault("node_artifacts", {})[self.name] = artifacts

            # 2b. Bind session — binding store replaces envelope transport
            # for graph_node_name / is_node_execution / graph_artifacts.
            if binding_store is not None and ctx.graph_instance_id is not None:
                binding_store.bind(
                    session.session_id,
                    SessionBinding(
                        task_id=ctx.graph_instance_id,
                        graph_node_name=self.name,
                        is_node_execution=True,
                        graph_artifacts=artifacts,
                    ),
                )
                bound_here = True

            # 3. Build input envelope (formats Origin Request + upstream input).
            envelope = await self._build_graph_input_envelope(
                ctx, integrated_input, session
            )

            # 4. Deliver to session inbox via tree — InboxPoller drives the
            # turn, cold-starting the agent from its template when no live
            # instance exists yet (SPEC §4 axis 3: same materialization
            # semantics as session mode).
            await tree.deliver(session.session_id, envelope, track_consume=True)

            # 5. Wait for the tree to quiesce (turn + any subagents complete).
            tree_id = await tree.tree_id_for_session(session.session_id)
            if tree_id is not None:
                await tree.wait_quiesce(tree_id)
            # return — no deliver check, no auto-deliver.
            # graph COMPLETED/FAILED is judged by ctx.reached_end (graph engine).
        finally:
            if bound_here and binding_store is not None:
                binding_store.unbind(session.session_id)
            if self._session_strategy is SessionStrategy.PER_INVOCATION:
                registry = await self._resolve_session_registry()
                await registry.cleanup(session.session_id)

    def _build_graph_artifacts(self, ctx: GraphContext[Any]) -> GraphTurnArtifacts:
        """Build GraphTurnArtifacts for the configurator pipeline."""
        deliver_tool = self._ensure_deliver_tool()
        topology_section = self._build_topology_section()
        node_description = self.resolve_description()
        if node_description == AgentNode.DESCRIPTION_NOT_FOUND:
            node_description = ""

        # Compute downstream target types for topology-aware prompt rendering.
        downstream_has_end = False
        downstream_has_agent = False
        if self._graph_ref is not None:
            for edge in self._graph_ref.edges_from(self.name):
                if edge.target == GraphNode.END:
                    downstream_has_end = True
                elif edge.target in self._graph_ref.nodes and isinstance(
                    self._graph_ref.nodes[edge.target], AgentNode
                ):
                    downstream_has_agent = True

        knowledge_dir: Path | None = None
        if self._knowledge_config.enabled and ctx.graph_instance_id is not None:
            workspace = self._workspace_resolver.resolve_workspace()
            knowledge_dir = workspace.ctx.paths.graph_instance_knowledge_dir(
                ctx.graph_instance_id
            )
            knowledge_dir.mkdir(parents=True, exist_ok=True)

        return GraphTurnArtifacts(
            deliver_tool=deliver_tool,
            topology_section=topology_section,
            node_description=node_description,
            knowledge_config=self._knowledge_config,
            knowledge_dir=knowledge_dir,
            downstream_has_agent=downstream_has_agent,
            downstream_has_end=downstream_has_end,
        )

    async def _build_graph_input_envelope(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
        session: SessionInfo,
    ) -> AgentMessageEnvelope:
        """Build the envelope delivered to the agent's inbox for this graph turn."""
        upstream = self._format_integrated_input(integrated_input)

        sections: list[str] = []
        if ctx.user_input is not None and ctx.user_input.content:
            sections.append("[Origin Request]:\n" + str(ctx.user_input.content))
        if upstream:
            sections.append(upstream)
        content = "\n\n".join(sections)

        return AgentMessageEnvelope(
            payload={"content": content},
            source=AgentAddress(name=self.name),
            target=AgentAddress(name=self._agent_name),
            message_type=AgentMessageType.EXTERNAL_INPUT,
            session_id=session.session_id_prefix,
            agent_session_id=session.session_id,
            metadata={
                "graph_instance_id": ctx.graph_instance_id,
            },
        )

    def _format_integrated_input(self, integrated_input: IntegratedInput) -> str:
        if not integrated_input.payloads:
            status = self._build_upstream_status(delivered_sources=set())
            return status

        groups: dict[str, list[str]] = {}
        source_descs: dict[str, str] = {}
        has_framework_sentinel = False
        for payload in integrated_input.payloads:
            source_name = self._resolve_source_name(payload.source_node)
            # __start__ payloads carry the user input that [Origin Request]
            # in execute() already renders — skip to avoid duplication.
            if source_name == GraphNode.START:
                continue
            # Framework sentinels (retry feedback, resume) replace the real
            # input — they are not upstream delivers. Track them to skip
            # the [Upstream Status] block, which would otherwise falsely
            # claim real upstreams delivered nothing.
            if source_name in FrameworkPayloadSource._value2member_map_:
                has_framework_sentinel = True
            content = payload.content
            text = content.content if hasattr(content, "content") else str(content)
            groups.setdefault(source_name, []).append(text)
            if source_name not in source_descs:
                source_descs[source_name] = self._resolve_upstream_desc(source_name)

        lines: list[str] = []
        for source_name, texts in groups.items():
            combined = "\n".join(texts)
            if source_name in FrameworkPayloadSource._value2member_map_:
                annotation = " (framework feedback — not from a graph node)"
            else:
                desc = source_descs.get(source_name, "")
                annotation = f" (upstream node, role: {desc})" if desc else " (upstream node)"
            lines.append(f"[Input from graph node '{source_name}']{annotation}:\n{combined}")

        if not has_framework_sentinel:
            delivered = set(groups.keys())
            status = self._build_upstream_status(delivered)
            if status:
                lines.append(status)

        return "\n\n".join(lines)

    def _resolve_source_name(self, node_id: str) -> str:
        # reverse-lookup node_id -> name via the compiled graph topology.
        if self._graph_ref is None:
            return node_id
        for name, node in self._graph_ref.nodes.items():
            if node.node_id == node_id:
                return name
        return node_id

    def _build_topology_section(self) -> str:
        """Render graph topology as markdown for the ### Topology subsection."""
        if self._graph_ref is None:
            return ""
        graph = self._graph_ref
        lines: list[str] = []
        lines.append(f"Graph: {graph.name}")
        lines.append(f"You are node: **{self.name}**")
        lines.append("")
        lines.append("Nodes:")
        for name in graph.nodes:
            if name == GraphNode.START:
                label = " (entry — receives Origin Request)"
            elif name == GraphNode.END:
                label = (
                    " (terminal — collects all upstream deliveries in order, "
                    "concatenates into the graph's final reply list)"
                )
            elif name == self.name:
                label = " ← YOU ARE HERE"
            else:
                label = ""
            lines.append(f"- {name}{label}")
        lines.append("")
        lines.append("Edges:")
        for edge in graph.edges:
            lines.append(f"- {edge.source} → {edge.target}")
        upstream = [e.source for e in graph.edges if e.target == self.name]
        downstream = [e.target for e in graph.edges_from(self.name)]
        lines.append("")
        lines.append(f"Your upstream (nodes that deliver to you): {', '.join(upstream) or '(none)'}")
        lines.append(f"Your downstream (nodes you can deliver to): {', '.join(downstream) or '(none)'}")
        lines.append("")
        lines.append(
            "Origin Request: the user's input that triggered this graph run. "
            "It enters through __start__ and is the root task every node works towards. "
            "You will see it in your input as [Origin Request]."
        )
        return "\n".join(lines)

    def _resolve_upstream_desc(self, source_name: str) -> str:
        """Resolve the role description of an upstream node.

        Returns empty string for non-AgentNode upstreams (no role_description
        available) — the annotation falls back to just '(upstream node)'.
        """
        if self._graph_ref is None:
            return ""
        node = self._graph_ref.nodes.get(source_name)
        if node is None:
            return ""
        if isinstance(node, AgentNode):
            desc = node.resolve_description()
            return "" if desc == AgentNode.DESCRIPTION_NOT_FOUND else desc
        return ""

    def _build_upstream_status(self, delivered_sources: set[str]) -> str:
        """Build the [Upstream Status] block showing delivered vs missing upstreams."""
        if self._graph_ref is None:
            return ""
        all_upstream = [
            e.source
            for e in self._graph_ref.edges
            if e.target == self.name and e.source != GraphNode.START
        ]
        if not all_upstream:
            return ""
        lines = ["[Upstream Status]"]
        for source in all_upstream:
            if source in delivered_sources:
                lines.append(f"- {source}: delivered")
            else:
                lines.append(
                    f"- {source}: no input — path not activated in this run, "
                    f"no further input expected. Proceed with received input."
                )
        return "\n".join(lines)

    def _ensure_deliver_tool(self) -> GraphDeliverTool:
        if self._deliver_tool is not None:
            return self._deliver_tool
        if self._graph_ref is None:
            raise RuntimeError(
                "Graph reference not set -- deliver tool requires graph topology"
            )
        store = GraphDeliverTargetStore(
            graph_ref=self._graph_ref,
            current_node=self.name,
        )
        self._deliver_tool = GraphDeliverTool(node=self, store=store)
        return self._deliver_tool


__all__ = ["BotAgentNode"]
