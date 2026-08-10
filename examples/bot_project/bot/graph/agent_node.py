"""BotAgentNode -- agent-backed graph node wiring pool/pipeline resources.

Bridges the static graph scheduling layer (modex_graph) to the bot's pool-mode
agent runtime. Each node binds a named agent from a named pool, resolves the
pool's TurnContextBuilder/ContextManager/Agent, and runs a full agent turn
inside ``execute``: session lifecycle, context assembly, integrated-input
injection, deliver-tool installation, agent execution, and auto-deliver.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.graph.knowledge_config import KnowledgeNodeConfig
from modex_agent.agents.agent_node import AgentNode, SessionStrategy
from modex_agent.core.message_utils import wrap_system_reminder
from modex_agent.core.tool_manager import Tool, ToolManager
from modex_agent.core.types import InputMessage, MessageRole
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.tools.graph_deliver import GraphDeliverTargetStore, GraphDeliverTool
from modex_agent.tools.graph_knowledge_capabilities import KnowledgeToolCapabilities
from modex_agent.tools.graph_knowledge_tool import GraphKnowledgeBaseTool
from modex_agent.tools.graph_tool_preset import GraphToolPreset
from modex_graph.constants import FrameworkPayloadSource, GraphNode
from modex_graph.integration import GraphPayload

if TYPE_CHECKING:
    from bot.workspace.handle import WorkspaceResolverCell
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.session_registry import SessionRegistry
    from modex_agent.multi_agent.descriptor import AgentInstance
    from modex_agent.multi_agent.pool_instance import PoolInstance
    from modex_graph.context import GraphContext
    from modex_graph.integration import IntegratedInput


class BotAgentNode(AgentNode):
    """Graph node backed by a pool agent with full turn-context lifecycle."""

    _THINK_PAIRED_RE = re.compile(
        r"<\s*(?:think|reasoning|reflection)\b[^>]*[>\n]"
        r"(.*?)</\s*(?:think|reasoning|reflection)\b[^>]*[>\n]",
        re.IGNORECASE | re.DOTALL,
    )
    _THINK_TAG_RE = re.compile(
        r"<\s*/?\s*(?:think|reasoning|reflection)\b[^>]*>?",
        re.IGNORECASE,
    )

    def __init__(
        self,
        agent_name: str,
        pool_name: str,
        workspace_resolver: WorkspaceResolverCell,
        *,
        session_strategy: SessionStrategy = SessionStrategy.CACHED,
        knowledge_config: KnowledgeNodeConfig | None = None,
    ) -> None:
        super().__init__(session_strategy=session_strategy)
        self._agent_name = agent_name
        self._pool_name = pool_name
        self._workspace_resolver = workspace_resolver
        self._deliver_tool: GraphDeliverTool | None = None
        self._knowledge_config = knowledge_config or KnowledgeNodeConfig()

    def agent_name(self) -> str:
        return self._agent_name

    async def _resolve_session_registry(self) -> SessionRegistry:
        pool = self._resolve_pool()
        registry: SessionRegistry | None = pool.pool.session_registry
        if registry is None:
            raise RuntimeError(f"Pool {self._pool_name!r} has no session registry")
        return registry

    def _resolve_pool(self) -> PoolInstance:
        workspace = self._workspace_resolver.resolve_workspace()
        pool = workspace.pools.get(self._pool_name)
        if pool is None:
            raise RuntimeError(f"Pool {self._pool_name!r} not found in workspace")
        return pool

    def _resolve_agent_instance(self) -> AgentInstance:
        pool = self._resolve_pool()
        instance: AgentInstance | None = pool.pool.get(self._agent_name)
        if instance is None:
            raise RuntimeError(
                f"Agent {self._agent_name!r} not found in pool {self._pool_name!r}"
            )
        return instance

    def resolve_description(self) -> str:
        instance = self._resolve_agent_instance()
        return instance.descriptor.role_description or AgentNode.DESCRIPTION_NOT_FOUND

    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> None:
        # Resolve pool resources.
        instance = self._resolve_agent_instance()
        pipeline = instance.pipeline
        if pipeline is None:
            raise RuntimeError(f"Agent {self._agent_name!r} has no pipeline")
        builder = pipeline._turn_context_builder
        if builder is None:
            raise RuntimeError(f"Agent {self._agent_name!r} has no TurnContextBuilder")
        ctx_mgr = instance.context_manager

        # Ensure session.
        session = await self._ensure_session(ctx)

        try:
            # Assemble context state.
            input_msg = InputMessage(content="", session=session)
            context_state = await builder.assemble(
                session_id=session.session_id,
                input_msg=input_msg,
                input_metadata={},
                sanitized_content=None,
                media_blocks=[],
                _media_processor=None,
                ctx_mgr=ctx_mgr,
                route_result=None,
                _is_approval_cmd=False,
                append_user_message=False,
            )

            # Build runtime and context.
            workspace = self._workspace_resolver.resolve_workspace()
            pool_data = workspace.pool_data.get(self._pool_name)
            agent_context, emitter = builder.build_runtime_and_context(
                session=session,
                context_state=context_state,
                ctx_mgr=ctx_mgr,
                input_metadata={},
                pool_data=pool_data,
            )

            upstream = self._format_integrated_input(integrated_input)
            existing_messages = await agent_context.history.to_list()
            is_re_execution = len(existing_messages) > 0

            if is_re_execution:
                # Crash recovery or resume — session already has [Origin Request]
                # from the prior invocation. Only inject new upstream input.
                if upstream:
                    reminder = wrap_system_reminder(upstream)
                else:
                    reminder = ""
            else:
                # First execution — append [Origin Request] + upstream input.
                sections: list[str] = []
                if ctx.user_input is not None and ctx.user_input.content:
                    sections.append("[Origin Request]:\n" + str(ctx.user_input.content))
                if upstream:
                    sections.append(upstream)
                reminder = wrap_system_reminder("\n\n".join(sections)) if sections else ""
            if reminder:
                await agent_context.history.append(
                    {"role": MessageRole.SYSTEM_REMINDER, "content": reminder}
                )

            deliver_tool = self._ensure_deliver_tool()
            graph_tools: list[Tool] = [deliver_tool]
            knowledge_dir: Path | None = None
            if self._knowledge_config.enabled and ctx.graph_instance_id is not None:
                knowledge_dir = workspace.ctx.paths.graph_instance_knowledge_dir(
                    ctx.graph_instance_id
                )
                knowledge_dir.mkdir(parents=True, exist_ok=True)
            knowledge_tool = self._ensure_knowledge_tool(
                knowledge_dir, agent_context.tool_manager
            )
            if knowledge_tool is not None:
                graph_tools.append(knowledge_tool)
            preset = GraphToolPreset(graph_tools=graph_tools)
            agent_context.tool_manager = preset.build_tool_manager(agent_context.tool_manager)

            # Set graph context for the deliver tool.
            agent_context.graph_context = ctx

            # Per-turn approval disable for graph context.
            #
            # Graph nodes run inside a pre-defined workflow — tools execute as
            # designed intent, not user-interacted agent actions. Approval
            # (DANGEROUS tier tool suspend) is a normal-session concept: it
            # protects users from agent-initiated risky actions in interactive
            # chat. In graph context, the workflow designer has already decided
            # which tools each node should use, so approval suspension would
            # deadlock the graph (no user to approve, no way to resume).
            #
            # This is a per-turn override on AgentRuntimeServices (a mutable
            # dataclass, NOT frozen). The pool-level base_services.approval is
            # NOT modified — the next normal session turn rebuilds a fresh
            # AgentRuntimeServices via build_runtime_and_context, which reads
            # approval from base_services again. Normal sessions are unaffected.
            if agent_context.runtime is not None:
                agent_context.runtime.services.approval = None

            # Run via TurnRunner.execute_turn — converges with normal turn lifecycle:
            # register_task + set_turn_uuid (session bookkeeping), ctx_mgr.save
            # (context persistence), finally: unregister_turn + _safe_flush +
            # on_session_end (cleanup). Hook/trace (BEFORE_GRAPH / FINALLY_GRAPH)
            # still fire inside agent.run() — execute_turn wraps, not replaces.
            runner = pipeline._turn_runner
            if not isinstance(runner, ReActTurnRunner):
                raise RuntimeError(
                    f"Agent {self._agent_name!r} requires a ReAct turn runner, "
                    f"got {type(runner).__name__}"
                )

            assert agent_context.runtime is not None, (
                "BotAgentNode requires agent_context.runtime to be set"
            )
            assert agent_context.runtime.state is not None, (
                "BotAgentNode requires agent_context.runtime.state to be set"
            )
            agent_context.runtime.state.custom[TurnCustomKey.MAX_TURNS] = 3
            agent_context.runtime.state.custom[TurnCustomKey.GRAPH_NODE_DESCRIPTION] = (
                self.resolve_description()
            )
            agent_context.runtime.state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = (
                self._build_topology_section()
            )
            if knowledge_dir is not None:
                agent_context.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_DIR] = str(
                    knowledge_dir
                )
                agent_context.runtime.state.custom[
                    TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ
                ] = self._knowledge_config.require_read
                agent_context.runtime.state.custom[
                    TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE
                ] = self._knowledge_config.require_write
            result = await runner.execute_turn(
                agent_context,
                emitter,
                session.session_id,
                context_state,
                input_metadata={},
                ctx_mgr=ctx_mgr,
            )
            assert result is not None

            # Auto-deliver if the agent did not explicitly deliver.
            if not self._has_pending_delivers():
                output = self._extract_auto_deliver_content(result)
                if output:
                    resolved = self._resolve_default_target(ctx, policy="graceful")
                    self.deliver(GraphPayload(content=output), resolved[0], ctx)
        finally:
            if self._session_strategy is SessionStrategy.PER_INVOCATION:
                registry = await self._resolve_session_registry()
                await registry.cleanup(session.session_id)

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

    def _extract_auto_deliver_content(self, result: AgentResult) -> str:
        raw = ""
        if result.messages:
            for msg in reversed(result.messages):
                if isinstance(msg, dict):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                else:
                    role = msg.role
                    content = msg.content
                if str(role) == "assistant" and content:
                    raw = str(content)
                    break
        if not raw:
            raw = result.content or ""
        return self._THINK_TAG_RE.sub("", self._THINK_PAIRED_RE.sub("", raw))

    def _has_pending_delivers(self) -> bool:
        return bool(self._pending_delivers)

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

    def _ensure_knowledge_tool(
        self,
        knowledge_dir: Path | None,
        tool_manager: ToolManager,
    ) -> GraphKnowledgeBaseTool | None:
        if knowledge_dir is None:
            return None
        capabilities = KnowledgeToolCapabilities.from_tool_manager(tool_manager)
        return GraphKnowledgeBaseTool(
            knowledge_dir=knowledge_dir,
            capabilities=capabilities,
            node_name=self.name,
        )


__all__ = ["BotAgentNode"]
