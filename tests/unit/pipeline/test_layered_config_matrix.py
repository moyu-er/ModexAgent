"""Layered configuration matrix tests.

Verifies the complete configuration distribution across all 8 combinations of
three orthogonal dimensions:

    Implementation: native (ReAct) / external (CLI)
    Topology:       normal (main) / subagent
    Mode:           session / graph

Plus the multi-agent communication data flow:
    - subagent dispatch (main→subagent): graph_instance_id propagation
    - subagent reply (subagent→parent): parent restores full graph config
    - peer send (cross-tree): graph_instance_id NOT propagated

See ``docs/design/session-tree/layered-config-matrix.md`` for the full design.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import AgentContext, AgentCommKind
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.history import ListMessageHistory
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
from modex_agent.hook.builtin.knowledge_hook import KnowledgeHook, _has_knowledge_config
from modex_agent.memory.history import ListMessageHistory
from modex_agent.memory.prompt_pipeline.providers import (
    GraphWorkflowProvider,
    _is_graph_node_execution,
)
from modex_agent.pipeline.turn_context_config import (
    GraphApprovalConfigurator,
    GraphContextBindingConfigurator,
    GraphKnowledgeConfigurator,
    GraphMaxTurnsConfigurator,
    GraphToolConfigurator,
    GraphTopologyConfigurator,
    TurnContextConfigPipeline,
    TurnContextDescriptor,
)
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.agents.react.state import ReActTurnState
from modex_graph.context import GraphContext
from modex_agent.runtime.enums import AgentKind, TurnPhase

def _graph_ctx():
    return MagicMock(spec=GraphContext)

from modex_agent.multi_agent.session_tree.session_binding import (
    InMemorySessionBindingStore,
    SessionBinding,
)
from modex_agent.multi_agent.communication.strategies.base import SendStrategy, SendDeps, SendRequest
from modex_agent.multi_agent.communication.strategies.subagent_dispatch import (
    SubagentDispatchStrategy,
)
from modex_agent.multi_agent.communication.strategies.parent_reply import (
    ParentReplyStrategy,
)
from modex_agent.multi_agent.communication.strategies.peer_normal import (
    PeerNormalStrategy,
)
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind as CommKind
from modex_agent.core.emitter import AgentResult
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.tool_manager import Tool, ToolConfig, InMemoryToolManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity(session: str = "conv1.main") -> TurnIdentity:
    return TurnIdentity(agent_id="test", session=SessionInfo.from_str(session), turn_id="t1")


def _state(session: str = "conv1.main") -> ReActTurnState:
    return ReActTurnState(
        identity=_identity(session),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        turn_attempt=1,
    )


def _ctx(
    *,
    graph_context: Any = None,
    graph_instance_id: int | None = None,
    state: ReActTurnState | None = None,
    tool_manager: Any = None,
    session: str = "conv1.main",
) -> AgentContext:
    s = state or _state(session)
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=s)
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=tool_manager or InMemoryToolManager(),
        session=SessionInfo.from_str(session),
        runtime=runtime,
        graph_context=graph_context,
        graph_instance_id=graph_instance_id,
        identity=_identity(session),
    )


def _full_graph_state(session: str = "conv1.main") -> ReActTurnState:
    """State with all graph keys set (as GraphTopologyConfigurator + GraphKnowledgeConfigurator would)."""
    s = _state(session)
    s.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] = "## topology"
    s.custom[TurnCustomKey.GRAPH_NODE_DESCRIPTION] = "node desc"
    s.custom[TurnCustomKey.GRAPH_KNOWLEDGE_DIR] = "/tmp/kb"
    s.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] = True
    s.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE] = False
    return s


def _subagent_graph_state(session: str = "inv1.sub") -> ReActTurnState:
    """State for a subagent in graph mode — graph_context is set but NO topology/knowledge keys."""
    s = _state(session)
    return s


def _full_binding(session: str = "conv1.main") -> SessionBinding:
    from modex_agent.pipeline.turn_context_config import GraphTurnArtifacts

    class _DeliverTool(Tool):
        @property
        def name(self) -> str:
            return "deliver"
        @property
        def description(self) -> str:
            return "deliver"
        @property
        def parameters(self) -> dict[str, Any]:
            return {}
        async def execute(self, **kwargs: Any) -> str:
            return "ok"

    return SessionBinding(
        task_id=42,
        graph_node_name="reviewer",
        is_node_execution=True,
        graph_artifacts=GraphTurnArtifacts(
            deliver_tool=_DeliverTool(),
            topology_section="## topology",
            node_description="node desc",
            knowledge_config=None,
            knowledge_dir=Path("/tmp/kb"),
        ),
    )


def _subagent_binding(session: str = "inv1.sub") -> SessionBinding:
    return SessionBinding(task_id=42)


def _config_pipeline() -> TurnContextConfigPipeline:
    return TurnContextConfigPipeline([
        GraphContextBindingConfigurator(),
        GraphApprovalConfigurator(),
        GraphMaxTurnsConfigurator(),
        GraphToolConfigurator(),
        GraphTopologyConfigurator(),
        GraphKnowledgeConfigurator(),
    ])


def _build_descriptor(
    *,
    agent_kind: AgentCommKind = AgentCommKind.NORMAL,
    binding: SessionBinding | None = None,
    graph_context: Any = None,
) -> TurnContextDescriptor:
    task_id = binding.task_id if binding is not None else None
    return TurnContextDescriptor(
        agent_kind=agent_kind,
        execution_strategy=ExecutionStrategyKind.REACT,
        graph_context=graph_context if binding is not None else None,
        graph_node_name=binding.graph_node_name if binding is not None else None,
        graph_instance_id=task_id,
        is_node_execution=binding.is_node_execution if binding is not None else False,
        graph_artifacts=binding.graph_artifacts if binding is not None else None,
    )


# ===========================================================================
# Part 1: Configurator matrix — 8 combinations
# ===========================================================================


class TestConfiguratorMatrix:
    """Verify configurator gate behavior for all 8 mode combinations."""

    def _apply_pipeline(
        self,
        desc: TurnContextDescriptor,
        ctx: AgentContext,
    ) -> None:
        pipeline = _config_pipeline()
        pipeline.configure(ctx, desc)

    def test_session_main_normal_no_graph_config(self) -> None:
        """Combo 1: native normal session — no graph config."""
        ctx = _ctx()
        desc = _build_descriptor(binding=None)
        self._apply_pipeline(desc, ctx)
        assert ctx.runtime is not None
        rt = ctx.runtime

        assert ctx.graph_instance_id is None
        assert ctx.graph_context is None
        assert ctx.runtime.state.custom.get(TurnCustomKey.MAX_TURNS) is None
        assert ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT) is None
        assert ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_DIR) is None
        assert ctx.tool_manager.get_tool("deliver") is None

    def test_graph_main_normal_full_graph_config(self) -> None:
        """Combo 2: native normal graph — full graph config."""
        graph_ctx = _graph_ctx()
        ctx = _ctx(graph_context=None)
        binding = _full_binding()
        desc = _build_descriptor(binding=binding, graph_context=graph_ctx)
        self._apply_pipeline(desc, ctx)
        assert ctx.runtime is not None
        rt = ctx.runtime

        assert ctx.graph_instance_id == 42
        assert ctx.graph_context is graph_ctx
        assert rt.services.approval is None
        assert rt.state.custom[TurnCustomKey.MAX_TURNS] == 3
        assert rt.state.custom[TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT] == "## topology"
        assert rt.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_DIR] is not None
        assert ctx.tool_manager.get_tool("deliver") is not None

    def test_session_subagent_no_graph_config(self) -> None:
        """Combo 3: native subagent session — no graph config."""
        ctx = _ctx(session="inv1.sub")
        desc = _build_descriptor(agent_kind=AgentCommKind.SUBAGENT, binding=None)
        self._apply_pipeline(desc, ctx)
        assert ctx.runtime is not None
        rt = ctx.runtime

        assert ctx.graph_instance_id is None
        assert ctx.graph_context is None
        assert ctx.tool_manager.get_tool("deliver") is None

    def test_graph_subagent_only_binding_and_approval(self) -> None:
        """Combo 4: native subagent graph — only ContextBinding + Approval, no tools/topology/knowledge."""
        graph_ctx = _graph_ctx()
        ctx = _ctx(session="inv1.sub")
        binding = _subagent_binding()
        desc = _build_descriptor(
            agent_kind=AgentCommKind.SUBAGENT, binding=binding, graph_context=graph_ctx
        )
        self._apply_pipeline(desc, ctx)
        assert ctx.runtime is not None
        rt = ctx.runtime

        assert ctx.graph_instance_id == 42
        assert ctx.graph_context is graph_ctx
        assert rt.services.approval is None

        assert rt.state.custom.get(TurnCustomKey.MAX_TURNS) is None
        assert rt.state.custom.get(TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT) is None
        assert rt.state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_DIR) is None
        assert ctx.tool_manager.get_tool("deliver") is None


# ===========================================================================
# Part 2: Graph-aware hooks/providers — subagent exclusion
# ===========================================================================


class TestGraphAwareComponentsSubagentExclusion:
    """Verify that graph-aware hooks and providers exclude subagents in graph mode."""

    async def test_graph_workflow_provider_no_graph_for_subagent_graph_mode(self) -> None:
        """GraphWorkflowProvider returns 'no-graph' for subagent in graph mode."""
        from modex_agent.core.agent import current_agent_context

        ctx = _ctx(
            graph_context=_graph_ctx(),
            graph_instance_id=42,
            state=_subagent_graph_state(),
        )
        token = current_agent_context.set(ctx)
        try:
            provider = GraphWorkflowProvider()
            version = await provider._fetch_version()
            content = await provider._fetch_content()
            assert version == "no-graph"
            assert content == ""
        finally:
            current_agent_context.reset(token)

    async def test_graph_workflow_provider_graph_for_main_graph_mode(self) -> None:
        """GraphWorkflowProvider returns 'graph' for main agent in graph mode."""
        from modex_agent.core.agent import current_agent_context

        ctx = _ctx(
            graph_context=_graph_ctx(),
            graph_instance_id=42,
            state=_full_graph_state(),
        )
        token = current_agent_context.set(ctx)
        try:
            provider = GraphWorkflowProvider()
            version = await provider._fetch_version()
            content = await provider._fetch_content()
            assert version == "graph"
            assert "## Graph Node Context" in content
            assert "### Topology" in content
        finally:
            current_agent_context.reset(token)

    async def test_knowledge_hook_noop_for_subagent_graph_mode(self) -> None:
        """KnowledgeHook.before_turn is no-op for subagent in graph mode."""
        ctx = _ctx(
            graph_context=_graph_ctx(),
            graph_instance_id=42,
            state=_subagent_graph_state(),
        )
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 5

        await KnowledgeHook().before_turn(ctx)

        # Counter NOT reset (hook skipped because no GRAPH_KNOWLEDGE_DIR)
        assert ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] == 5

    async def test_knowledge_hook_active_for_main_graph_mode(self) -> None:
        """KnowledgeHook.before_turn resets counters for main agent in graph mode."""
        ctx = _ctx(
            graph_context=_graph_ctx(),
            graph_instance_id=42,
            state=_full_graph_state(),
        )
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 5

        await KnowledgeHook().before_turn(ctx)

        assert ctx.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] == 0

    async def test_deliver_retry_hook_noop_for_subagent(self) -> None:
        """DeliverRetryHook is no-op for subagent (no deliver tool)."""
        ctx = _ctx(
            graph_context=_graph_ctx(),
            graph_instance_id=42,
            state=_subagent_graph_state(),
            tool_manager=InMemoryToolManager(),  # no deliver tool
        )
        result = AgentResult(stop_reason=StopReason.COMPLETED, content="done")
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_DELIVER_COUNT] = 0

        await DeliverRetryHook().after_turn(ctx, result)

        # No continuation request (hook skipped)
        assert not ctx.runtime.state.custom.get("_continuation_request", False)

    async def test_deliver_retry_hook_active_for_main_graph(self) -> None:
        """DeliverRetryHook fires for main agent with deliver tool but no deliver call."""
        from modex_agent.core.tool_manager import Tool, ToolConfig

        class _StubDeliverTool(Tool):
            @property
            def name(self) -> str:
                return "deliver"
            @property
            def description(self) -> str:
                return "deliver tool"
            @property
            def parameters(self) -> dict[str, Any]:
                return {}

            async def execute(self, **kwargs: Any) -> str:
                return "ok"

        tm = InMemoryToolManager()
        tm.register(_StubDeliverTool())
        ctx = _ctx(
            graph_context=_graph_ctx(),
            graph_instance_id=42,
            state=_full_graph_state(),
            tool_manager=tm,
        )
        ctx.runtime.state.custom[TurnCustomKey.MAX_TURNS] = 3
        ctx.runtime.state.custom[TurnCustomKey.GRAPH_DELIVER_COUNT] = 0
        result = AgentResult(stop_reason=StopReason.COMPLETED, content="done")

        await DeliverRetryHook().after_turn(ctx, result)

        # Continuation requested (no deliver called, budget allows)
        assert ctx.runtime.state.custom.get("_continuation_request") is True

    def test_is_graph_node_execution_helper(self) -> None:
        """_is_graph_node_execution checks GRAPH_TOPOLOGY_CONTEXT, not just graph_context."""
        # No graph_context → False
        ctx = _ctx(graph_context=None)
        assert _is_graph_node_execution(ctx) is False

        # graph_context but no topology key → False (subagent in graph mode)
        ctx = _ctx(graph_context=MagicMock(), state=_subagent_graph_state())
        assert _is_graph_node_execution(ctx) is False

        # graph_context + topology key → True (main agent in graph mode)
        ctx = _ctx(graph_context=MagicMock(), state=_full_graph_state())
        assert _is_graph_node_execution(ctx) is True

    def test_has_knowledge_config_helper(self) -> None:
        """_has_knowledge_config checks GRAPH_KNOWLEDGE_DIR, not just graph_context."""
        ctx = _ctx(graph_context=None)
        assert _has_knowledge_config(ctx) is False

        ctx = _ctx(graph_context=MagicMock(), state=_subagent_graph_state())
        assert _has_knowledge_config(ctx) is False

        ctx = _ctx(graph_context=MagicMock(), state=_full_graph_state())
        assert _has_knowledge_config(ctx) is True


# ===========================================================================
# Part 3: Multi-agent communication — graph_instance_id propagation
# ===========================================================================


def _make_send_deps(tree: Any = None) -> SendDeps:
    return SendDeps(
        source=AgentAddress(name="main"),
        session_factory=MagicMock(),
        tree=tree or MagicMock(),
        session_registry=None,
    )


class TestGraphInstanceIdPropagation:
    """Verify graph_instance_id propagation rules across communication strategies."""

    def test_subagent_dispatch_propagates_graph_instance_id(self) -> None:
        """SubagentDispatchStrategy.should_propagate_graph_instance_id → True."""
        deps = _make_send_deps()
        strategy = SubagentDispatchStrategy(deps)
        assert strategy.should_propagate_graph_instance_id() is True

    def test_parent_reply_propagates_graph_instance_id(self) -> None:
        """ParentReplyStrategy.should_propagate_graph_instance_id → True."""
        deps = _make_send_deps()
        strategy = ParentReplyStrategy(deps)
        assert strategy.should_propagate_graph_instance_id() is True

    def test_peer_normal_does_not_propagate_graph_instance_id(self) -> None:
        """PeerNormalStrategy.should_propagate_graph_instance_id → False (cross-tree)."""
        deps = _make_send_deps()
        strategy = PeerNormalStrategy(deps)
        assert strategy.should_propagate_graph_instance_id() is False

    async def test_subagent_dispatch_execute_injects_gid_into_envelope(self) -> None:
        """SubagentDispatchStrategy.execute stamps graph_instance_id on envelope when sender has it."""
        tree = MagicMock()
        tree.deliver = MagicMock(return_value=None)
        deps = _make_send_deps(tree=tree)

        strategy = SubagentDispatchStrategy(deps)
        ctx = _ctx(graph_instance_id=42)
        target = CommunicationTarget(
            name="explore", kind=CommKind.SUBAGENT,
        )
        req = SendRequest(
            target=target, content="do task", invocation_id=None, context=ctx,
        )

        captured_envelopes: list[Any] = []
        async def _capture_deliver(sid: str, env: Any) -> None:
            captured_envelopes.append(env)

        tree.deliver = _capture_deliver

        await strategy.execute(req)

        assert len(captured_envelopes) == 1
        assert captured_envelopes[0].metadata.get("graph_instance_id") == 42

    async def test_peer_normal_execute_does_not_inject_gid(self) -> None:
        """PeerNormalStrategy.execute does NOT stamp graph_instance_id (cross-tree)."""
        peer_tree = MagicMock()

        captured_envelopes: list[Any] = []
        async def _capture_deliver(sid: str, env: Any) -> None:
            captured_envelopes.append(env)

        peer_tree.deliver = _capture_deliver

        deps = _make_send_deps(tree=peer_tree)
        strategy = PeerNormalStrategy(deps)
        ctx = _ctx(graph_instance_id=99)
        target = CommunicationTarget(
            name="peer_main", kind=CommKind.NORMAL, tree_ref=peer_tree,
        )
        req = SendRequest(
            target=target, content="hello", invocation_id=None, context=ctx,
        )

        await strategy.execute(req)

        assert len(captured_envelopes) == 1
        assert "graph_instance_id" not in captured_envelopes[0].metadata


# ===========================================================================
# Part 4: Binding store lifecycle — subagent round-trip
# ===========================================================================


class TestBindingStoreRoundTrip:
    """Verify binding store correctly maintains graph config across a subagent round-trip."""

    def test_parent_binding_survives_subagent_reply(self) -> None:
        """Parent's full binding is NOT overwritten when subagent replies.

        Simulates:
        1. BotAgentNode.execute creates full binding for main_session
        2. Subagent dispatch creates task_id-only binding for subagent_session
        3. SubagentAutoSendHook delivers back to main_session
        4. main_session binding must still be the full one (not overwritten)
        """
        store = InMemorySessionBindingStore()
        main_sid = "conv1.main"
        sub_sid = "inv1.sub"

        # Step 1: BotAgentNode creates full binding
        full = _full_binding(main_sid)
        store.bind(main_sid, full)
        assert store.get(main_sid) is full

        # Step 2: tree.deliver auto-creates subagent binding (task_id only)
        # (simulated — _maybe_bind_session would do this)
        store.bind(sub_sid, _subagent_binding(sub_sid))
        assert store.get(sub_sid).task_id == 42
        assert store.get(sub_sid).graph_node_name is None

        # Step 3: subagent replies → tree.deliver back to main_session
        # _maybe_bind_session checks: existing binding → skip (don't overwrite)
        # (simulated by NOT calling bind again — _maybe_bind_session checks get() first)
        existing = store.get(main_sid)
        assert existing is full  # still the full binding

        # Step 4: parent woken → _build_turn_descriptor reads binding
        binding = store.get(main_sid)
        assert binding.task_id == 42
        assert binding.graph_node_name == "reviewer"
        assert binding.is_node_execution is True
        assert binding.graph_artifacts is not None

    def test_subagent_binding_is_task_id_only(self) -> None:
        """Subagent binding has task_id but no graph_node_name/is_node_execution/artifacts."""
        store = InMemorySessionBindingStore()
        store.bind("inv1.sub", _subagent_binding())
        b = store.get("inv1.sub")
        assert b.task_id == 42
        assert b.graph_node_name is None
        assert b.is_node_execution is False
        assert b.graph_artifacts is None

    def test_unbind_cleans_up(self) -> None:
        """unbind removes the binding; get returns None."""
        store = InMemorySessionBindingStore()
        store.bind("conv1.main", _full_binding())
        assert store.get("conv1.main") is not None
        store.unbind("conv1.main")
        assert store.get("conv1.main") is None

    def test_unbind_nonexistent_is_noop(self) -> None:
        """unbind on a session with no binding is a no-op."""
        store = InMemorySessionBindingStore()
        store.unbind("never_bound")  # should not raise


# ===========================================================================
# Part 5: External agent — graph_instance_id only, no graph tools/hooks/providers
# ===========================================================================


class TestExternalAgentGraphMode:
    """Verify external agents get graph_instance_id only, no graph tools/hooks/providers."""

    def test_external_context_has_graph_instance_id_only(self) -> None:
        """ExternalTurnRunner sets graph_instance_id but not graph_context/tools/hooks."""
        # Simulate what ExternalTurnRunner.process_locked does
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),  # empty
            session=SessionInfo.from_str("conv1.main"),
        )
        # ExternalTurnRunner reads from binding store
        store = InMemorySessionBindingStore()
        store.bind("conv1.main", SessionBinding(task_id=42))
        binding = store.get("conv1.main")
        if binding is not None and binding.task_id is not None:
            ctx.graph_instance_id = binding.task_id

        assert ctx.graph_instance_id == 42
        assert ctx.graph_context is None
        assert ctx.runtime is None
        assert ctx.tool_manager.get_tool("deliver") is None


# ===========================================================================
# Part 6: Peer communication — graph mode hides peers, session mode cross-tree
# ===========================================================================


class TestPeerCommunicationGraphMode:
    """Graph mode: peer targets invisible via CommunicationTargetStore gates."""

    def _make_target_store(self) -> Any:
        from modex_agent.multi_agent.tools import CommunicationTargetStore

        store = CommunicationTargetStore(for_subagent=False)
        store.add(CommunicationTarget(name="peer_a", kind=AgentCommKind.NORMAL, description="peer A"))
        store.add(CommunicationTarget(name="sub_b", kind=AgentCommKind.SUBAGENT, description="sub B"))
        return store

    def test_graph_mode_hides_normal_targets_in_list(self) -> None:
        from modex_agent.core.agent import current_agent_context

        store = self._make_target_store()
        ctx = _ctx(graph_instance_id=42, graph_context=_graph_ctx())
        token = current_agent_context.set(ctx)
        try:
            targets = store.list()
            kinds = {t.kind for t in targets}
            assert AgentCommKind.NORMAL not in kinds
            assert AgentCommKind.SUBAGENT in kinds
        finally:
            current_agent_context.reset(token)

    def test_graph_mode_get_returns_none_for_normal_target(self) -> None:
        from modex_agent.core.agent import current_agent_context

        store = self._make_target_store()
        ctx = _ctx(graph_instance_id=42, graph_context=_graph_ctx())
        token = current_agent_context.set(ctx)
        try:
            assert store.get("peer_a") is None
            assert store.has("peer_a") is False
        finally:
            current_agent_context.reset(token)

    def test_graph_mode_subagent_target_still_visible(self) -> None:
        from modex_agent.core.agent import current_agent_context

        store = self._make_target_store()
        ctx = _ctx(graph_instance_id=42, graph_context=_graph_ctx())
        token = current_agent_context.set(ctx)
        try:
            assert store.get("sub_b") is not None
            assert store.has("sub_b") is True
        finally:
            current_agent_context.reset(token)

    def test_session_mode_shows_all_targets(self) -> None:
        from modex_agent.core.agent import current_agent_context

        store = self._make_target_store()
        ctx = _ctx(graph_instance_id=None, graph_context=None)
        token = current_agent_context.set(ctx)
        try:
            targets = store.list()
            names = {t.name for t in targets}
            assert "peer_a" in names
            assert "sub_b" in names
        finally:
            current_agent_context.reset(token)


class TestPeerCommunicationSessionCrossTree:
    """Session mode: peer send delivers to receiver's tree (cross-tree), no graph contamination."""

    async def test_peer_deliver_uses_target_tree_ref(self) -> None:
        from modex_agent.core.agent import current_agent_context

        peer_tree = MagicMock()
        delivered_to: list[tuple[str, Any]] = []

        async def _peer_deliver(sid: str, env: Any) -> None:
            delivered_to.append((sid, env))

        peer_tree.deliver = _peer_deliver

        deps = _make_send_deps(tree=peer_tree)
        strategy = PeerNormalStrategy(deps)
        target = CommunicationTarget(
            name="peer_main",
            kind=AgentCommKind.NORMAL,
            tree_ref=peer_tree,
        )
        ctx = _ctx(graph_instance_id=None, graph_context=None)
        req = SendRequest(target=target, content="hello", invocation_id=None, context=ctx)

        await strategy.execute(req)

        assert len(delivered_to) == 1
        env = delivered_to[0][1]
        assert "graph_instance_id" not in env.metadata

    async def test_peer_deliver_falls_back_to_deps_tree(self) -> None:
        local_tree = MagicMock()
        delivered: list[Any] = []

        async def _local_deliver(sid: str, env: Any) -> None:
            delivered.append(env)

        local_tree.deliver = _local_deliver

        deps = _make_send_deps(tree=local_tree)
        strategy = PeerNormalStrategy(deps)
        target = CommunicationTarget(name="peer_main", kind=AgentCommKind.NORMAL)
        ctx = _ctx(graph_instance_id=None, graph_context=None)
        req = SendRequest(target=target, content="hello", invocation_id=None, context=ctx)

        await strategy.execute(req)

        assert len(delivered) == 1

