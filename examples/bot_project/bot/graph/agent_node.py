"""BotAgentNode -- agent-backed graph node wiring pool/pipeline resources.

Bridges the static graph scheduling layer (modex_graph) to the bot's pool-mode
agent runtime. Each node binds a named agent from a named pool, resolves the
pool's TurnContextBuilder/ContextManager/Agent, and runs a full agent turn
inside ``execute``: session lifecycle, context assembly, integrated-input
injection, deliver-tool installation, agent execution, and auto-deliver.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from modex_agent.agents.agent_node import AgentNode, SessionStrategy
from modex_agent.core.message_utils import wrap_system_reminder
from modex_agent.core.types import InputMessage, MessageRole
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.tools.graph_deliver import GraphDeliverTargetStore, GraphDeliverTool
from modex_agent.tools.graph_tool_preset import GraphToolPreset
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
    ) -> None:
        super().__init__(session_strategy=session_strategy)
        self._agent_name = agent_name
        self._pool_name = pool_name
        self._workspace_resolver = workspace_resolver
        self._deliver_tool: GraphDeliverTool | None = None

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
        return instance.descriptor.role_description or "[not found]"

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

            sections: list[str] = []
            if ctx.user_input is not None and ctx.user_input.content:
                sections.append("[Origin Request]:\n" + str(ctx.user_input.content))
            if integrated_input.payloads:
                upstream = self._format_integrated_input(integrated_input)
                if upstream:
                    sections.append(upstream)
            reminder = wrap_system_reminder("\n\n".join(sections)) if sections else ""
            if reminder:
                await agent_context.history.append(
                    {"role": MessageRole.SYSTEM_REMINDER, "content": reminder}
                )

            # Ensure deliver tool and override tool manager.
            deliver_tool = self._ensure_deliver_tool()
            preset = GraphToolPreset(graph_tools=[deliver_tool])
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
            # on_session_end (cleanup). Hook/trace (BEFORE_TURN / FINALLY_TURN)
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
            return ""
        groups: dict[str, list[str]] = {}
        for payload in integrated_input.payloads:
            source_name = self._resolve_source_name(payload.source_node)
            content = payload.content
            text = content.content if hasattr(content, "content") else str(content)
            groups.setdefault(source_name, []).append(text)
        lines: list[str] = []
        for source_name, texts in groups.items():
            combined = "\n".join(texts)
            lines.append(f"[Input from graph node '{source_name}']:\n{combined}")
        return "\n\n".join(lines)

    def _resolve_source_name(self, node_id: str) -> str:
        # reverse-lookup node_id -> name via the compiled graph topology.
        if self._graph_ref is None:
            return node_id
        for name, node in self._graph_ref.nodes.items():
            if node.node_id == node_id:
                return name
        return node_id

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


__all__ = ["BotAgentNode"]
