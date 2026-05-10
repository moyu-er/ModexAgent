"""Verification tests for ReAct Graph refactoring — critical scenarios from spec & code review.

Tests the full approval flow, suspend-resume lifecycle, memory isolation,
event contract, engine routing, and bot_project wiring.
"""

import pytest
from framework.agents.react.approval import TieredToolApprovalClassifier
from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig
from framework.approval.constants import ApprovalDecision, ApprovalTier, ApprovalStatus
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.approval.store import InMemoryApprovalStateStore
from framework.approval.response import parse_approval_action
from framework.approval.types import ApprovalAction
from framework.agents.react.strategy import InlineWaitStrategy
from framework.agents.react.constants import ReActNode, ReActReason
from framework.agents.react.nodes.tool import ToolNode
from framework.agents.react.nodes.start import StartNode
from framework.agents.react.nodes.llm import LLMNode
from framework.agents.react.nodes.end import EndNode
from framework.agents.react.graph import ReActGraph
from framework.agents.react.agent import ReActEvent
from framework.core.graph.node import NodeTransition
from framework.core.graph.graph import Graph, Edge
from framework.core.graph.engine import GraphEngine
from framework.core.graph.constants import GraphNode, GraphMetaKey
from framework.core.agent import AgentContext
from framework.core.graph.interrupt import GraphInterrupt

from framework.core.tool_manager import InMemoryToolManager
from framework.core.emitter import ToolCall, ToolResult
from framework.core.types import LLMResponse
from framework.core.constants import FinishReason
from framework.memory.history import ListMessageHistory
from framework.hook import HookPoint


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Approval System: TieredToolApprovalClassifier.classify
# ═══════════════════════════════════════════════════════════════════════════════

class TestClassifyTier:
    """Verify TieredToolApprovalClassifier.classify correctly classifies tools."""

    def test_disabled_returns_normal(self):
        from framework.approval.config import AgentApprovalConfig
        classifier = TieredToolApprovalClassifier(config=AgentApprovalConfig(enabled=False))
        tc = ToolCall(tool_name="cat", call_id="c1", arguments={})
        ctx = AgentContext(system_prompt="test", history=ListMessageHistory(),
                            tool_manager=InMemoryToolManager())
        assert classifier.classify(tc, ctx) == ApprovalTier.NORMAL

    def test_unconfigured_tool_returns_normal(self):
        from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig
        config = AgentApprovalConfig(
            enabled=True,
            tools={"rm": ToolApprovalConfig(allowed_paths=[])},
        )
        classifier = TieredToolApprovalClassifier(config=config)
        tc = ToolCall(tool_name="cat", call_id="c1", arguments={})
        ctx = AgentContext(system_prompt="test", history=ListMessageHistory(),
                            tool_manager=InMemoryToolManager())
        assert classifier.classify(tc, ctx) == ApprovalTier.NORMAL

    def test_configured_tool_with_empty_paths_returns_dangerous(self):
        from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig
        config = AgentApprovalConfig(
            enabled=True,
            tools={"rm": ToolApprovalConfig(allowed_paths=[])},
        )
        classifier = TieredToolApprovalClassifier(config=config)
        tc = ToolCall(tool_name="rm", call_id="c1", arguments={"path": "/tmp/x"})
        ctx = AgentContext(system_prompt="test", history=ListMessageHistory(),
                            tool_manager=InMemoryToolManager())
        assert classifier.classify(tc, ctx) == ApprovalTier.DANGEROUS

    def test_configured_tool_with_star_paths_returns_normal(self):
        from pathlib import Path
        from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig
        from framework.interceptor.builtin.tool_approval import ArgumentMatcher
        config = AgentApprovalConfig(
            enabled=True,
            tools={"read_file": ToolApprovalConfig(allowed_paths=["*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        classifier = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="read_file", call_id="c1", arguments={"path": "/etc/passwd"})
        ctx = AgentContext(system_prompt="test", history=ListMessageHistory(),
                            tool_manager=InMemoryToolManager())
        assert classifier.classify(tc, ctx) == ApprovalTier.NORMAL

    def test_no_config_all_normal(self):
        from framework.approval.config import AgentApprovalConfig
        classifier = TieredToolApprovalClassifier(config=AgentApprovalConfig())
        tc = ToolCall(tool_name="anything", call_id="c1", arguments={})
        ctx = AgentContext(system_prompt="test", history=ListMessageHistory(),
                            tool_manager=InMemoryToolManager())
        assert classifier.classify(tc, ctx) == ApprovalTier.NORMAL


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ToolNode classification via interceptor chain
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolNodeClassification:
    """Verify ToolNode._classify_all correctly uses runtime.approval.classifier."""

    def _make_ctx(self, classifier=None):
        from unittest.mock import MagicMock
        from framework.agents.react.runtime import ReActRuntime
        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )
        runtime = ReActRuntime(mode="full")
        if classifier is not None:
            runtime.approval = MagicMock(classifier=classifier)
        ctx.runtime = runtime
        return ctx

    def test_classifies_dangerous_as_pending(self):
        from unittest.mock import MagicMock
        agent = MagicMock()

        classifier = MagicMock()
        classifier.classify.return_value = ApprovalTier.DANGEROUS

        node = ToolNode(agent)
        ctx = self._make_ctx(classifier=classifier)
        tc = ToolCall(tool_name="rm", call_id="c1", arguments={})
        decisions = node._classify_all([tc], ctx)

        assert decisions == [ApprovalDecision.PENDING]

    def test_classifies_normal_as_allowed(self):
        from unittest.mock import MagicMock
        agent = MagicMock()

        classifier = MagicMock()
        classifier.classify.return_value = ApprovalTier.NORMAL

        node = ToolNode(agent)
        ctx = self._make_ctx(classifier=classifier)
        tc = ToolCall(tool_name="cat", call_id="c1", arguments={})
        decisions = node._classify_all([tc], ctx)

        assert decisions == [ApprovalDecision.ALLOWED]

    def test_classifies_hardline_as_denied(self):
        from unittest.mock import MagicMock
        agent = MagicMock()

        classifier = MagicMock()
        classifier.classify.return_value = ApprovalTier.HARDLINE

        node = ToolNode(agent)
        ctx = self._make_ctx(classifier=classifier)
        tc = ToolCall(tool_name="sudo", call_id="c1", arguments={})
        decisions = node._classify_all([tc], ctx)

        assert decisions == [ApprovalDecision.DENIED]

    def test_no_runtime_all_allowed(self):
        from unittest.mock import MagicMock
        agent = MagicMock()
        node = ToolNode(agent)
        ctx = self._make_ctx()  # no approval runtime
        tc = ToolCall(tool_name="anything", call_id="c1", arguments={})
        decisions = node._classify_all([tc], ctx)

        assert decisions == [ApprovalDecision.ALLOWED]

    def test_no_classifier_all_allowed(self):
        from unittest.mock import MagicMock
        agent = MagicMock()
        node = ToolNode(agent)
        ctx = self._make_ctx()
        # runtime exists but approval is None
        ctx.runtime.approval = None
        tc = ToolCall(tool_name="anything", call_id="c1", arguments={})
        decisions = node._classify_all([tc], ctx)

        assert decisions == [ApprovalDecision.ALLOWED]

    def test_mixed_classification(self):
        from unittest.mock import MagicMock
        agent = MagicMock()

        def classify(tc, ctx):
            if tc.tool_name == "rm":
                return ApprovalTier.DANGEROUS
            if tc.tool_name == "cat":
                return ApprovalTier.NORMAL
            if tc.tool_name == "sudo":
                return ApprovalTier.HARDLINE
            return ApprovalTier.NORMAL

        classifier = MagicMock()
        classifier.classify.side_effect = classify

        node = ToolNode(agent)
        ctx = self._make_ctx(classifier=classifier)
        tcs = [
            ToolCall(tool_name="cat", call_id="c1", arguments={}),
            ToolCall(tool_name="rm", call_id="c2", arguments={}),
            ToolCall(tool_name="sudo", call_id="c3", arguments={}),
        ]
        decisions = node._classify_all(tcs, ctx)

        assert decisions == [
            ApprovalDecision.ALLOWED,
            ApprovalDecision.PENDING,
            ApprovalDecision.DENIED,
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ApprovalState cascade logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestApprovalStateCascade:
    """Verify ApprovalState.apply() cascade and final_decisions()."""

    def _make_state(self, n=3):
        reqs = [
            ApprovalRequest(f"t{i}", f"c{i}", {}, "dangerous", 1)
            for i in range(1, n + 1)
        ]
        return ApprovalState(session_id="s1", requests=reqs)

    def test_all_allowed(self):
        state = self._make_state(2)
        state.apply("c1", ApprovalDecision.ALLOWED)
        state.apply("c2", ApprovalDecision.ALLOWED)
        assert state.every_tool_decided is True
        assert state.status == ApprovalStatus.APPROVED
        assert state.final_decisions() == [
            ApprovalDecision.ALLOWED, ApprovalDecision.ALLOWED,
        ]

    def test_first_denied_cascades_to_preempted(self):
        state = self._make_state(3)
        state.apply("c1", ApprovalDecision.DENIED)
        assert state.final_decisions() == [
            ApprovalDecision.DENIED,
            ApprovalDecision.PREEMPTED,
            ApprovalDecision.PREEMPTED,
        ]
        assert state.status == ApprovalStatus.DENIED

    def test_middle_denied_cascades_rest(self):
        state = self._make_state(3)
        state.apply("c1", ApprovalDecision.ALLOWED)
        state.apply("c2", ApprovalDecision.DENIED)
        assert state.final_decisions() == [
            ApprovalDecision.PREEMPTED,  # Batch atomicity: ALLOWED → PREEMPTED on deny
            ApprovalDecision.DENIED,
            ApprovalDecision.PREEMPTED,
        ]

    def test_partial_approval(self):
        state = self._make_state(3)
        state.apply("c1", ApprovalDecision.ALLOWED)
        assert state.every_tool_decided is False
        assert state.unresolved_count == 2
        assert state.status == ApprovalStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Memory Isolation: approval commands NOT in memory
# ═══════════════════════════════════════════════════════════════════════════════

class TestMemoryIsolation:
    """Verify system prompt separation and approval command parsing."""

    @pytest.mark.asyncio
    async def test_to_messages_excludes_system_prompt(self):
        ctx = AgentContext(
            system_prompt="You are a bot",
            history=ListMessageHistory([
                {"role": "user", "content": "hello"},
            ]),
            tool_manager=InMemoryToolManager(),
        )
        msgs = await ctx.to_messages()
        roles = [m["role"] for m in msgs]
        assert "system" not in roles
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_to_messages_filters_stale_system(self):
        ctx = AgentContext(
            system_prompt="Live prompt",
            history=ListMessageHistory([
                {"role": "system", "content": "stale system msg"},
                {"role": "user", "content": "hi"},
            ]),
            tool_manager=InMemoryToolManager(),
        )
        msgs = await ctx.to_messages()
        roles = [m["role"] for m in msgs]
        assert "system" not in roles
        assert {"role": "user", "content": "hi"} in msgs

    def test_parse_approve(self):
        assert parse_approval_action("/approve") == ApprovalAction.ALLOW
        assert parse_approval_action("approve") == ApprovalAction.ALLOW
        assert parse_approval_action("/allow") == ApprovalAction.ALLOW
        assert parse_approval_action("ALLOW") == ApprovalAction.ALLOW

    def test_parse_deny(self):
        assert parse_approval_action("/deny") == ApprovalAction.DENY
        assert parse_approval_action("deny") == ApprovalAction.DENY
        assert parse_approval_action("/reject") == ApprovalAction.DENY
        assert parse_approval_action("DENY") == ApprovalAction.DENY

    def test_parse_non_approval_returns_none(self):
        assert parse_approval_action("hello world") is None
        assert parse_approval_action("this is a very long message that exceeds thirty characters") is None
        assert parse_approval_action("/help") is None


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Graph Engine routing
# ═══════════════════════════════════════════════════════════════════════════════

class TestGraphEngineRouting:
    """Verify GraphEngine correctly routes through nodes and builds results."""

    @pytest.mark.asyncio
    async def test_simple_path_routing(self):
        g = Graph()
        call_order = []

        class _Node:
            def __init__(self, name, next_target, next_reason="done"):
                self.name = name
                self._next = NodeTransition(next_target, next_reason)

            async def execute(self, ctx):
                call_order.append(self.name)
                return self._next

        g.add_node(_Node("start", "b", "go"))
        g.add_node(_Node("b", GraphNode.END, "done"))
        g.add_edge("start", "b", reason="go")

        ctx = type("Ctx", (), {"metadata": {}})()
        await GraphEngine(g).run(ctx)
        assert call_order == ["start", "b"]

    @pytest.mark.asyncio
    async def test_graph_interrupt_propagates(self):
        g = Graph()

        class _RaiseNode:
            def __init__(self):
                self.name = "start"

            async def execute(self, ctx):
                raise GraphInterrupt(value="pause", node_name="start", iteration=0)

        g.add_node(_RaiseNode())
        ctx = type("Ctx", (), {"metadata": {}})()

        with pytest.raises(GraphInterrupt) as exc_info:
            await GraphEngine(g).run(ctx)
        assert exc_info.value.value == "pause"

    @pytest.mark.asyncio
    async def test_build_result_overridable(self):
        class MyEngine(GraphEngine):
            def build_result(self, ctx):
                return "custom_result"

        g = Graph()

        class _Node:
            def __init__(self):
                self.name = "start"

            async def execute(self, ctx):
                return NodeTransition(GraphNode.END, "done")

        g.add_node(_Node())
        ctx = type("Ctx", (), {"metadata": {}})()
        result = await MyEngine(g).run(ctx)
        assert result == "custom_result"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. ReActGraph Clean vs Full
# ═══════════════════════════════════════════════════════════════════════════════

class TestReActGraphModes:
    """Verify ReActGraph builds correct topology for clean and full modes."""

    def test_full_mode_all_edges(self):
        g = ReActGraph(None, mode="full")
        assert g.next_node(ReActNode.START, ReActReason.NORMAL_START) == ReActNode.LLM
        assert g.next_node(ReActNode.START, ReActReason.RESUME_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.HAS_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.NO_TOOLS) == ReActNode.END
        assert g.next_node(ReActNode.LLM, ReActReason.MAX_ITERATIONS) == ReActNode.END
        assert g.next_node(ReActNode.TOOL, ReActReason.TOOLS_DONE) == ReActNode.LLM
        assert g.next_node(ReActNode.TOOL, ReActReason.TURN_CANCELLED) == ReActNode.END

    def test_clean_mode_all_nodes_exist(self):
        g = ReActGraph(None, mode="clean")
        assert ReActNode.START in g._nodes
        assert ReActNode.LLM in g._nodes
        assert ReActNode.TOOL in g._nodes
        assert ReActNode.END in g._nodes

    def test_full_mode_all_nodes_exist(self):
        g = ReActGraph(None, mode="full")
        assert ReActNode.START in g._nodes
        assert ReActNode.LLM in g._nodes
        assert ReActNode.TOOL in g._nodes
        assert ReActNode.END in g._nodes


# ═══════════════════════════════════════════════════════════════════════════════
# 10. LLMNode message assembly
# ═══════════════════════════════════════════════════════════════════════════════

class TestLLMNodeMessages:
    """Verify LLMNode assembles messages with system prompt prepended."""

    @pytest.mark.asyncio
    async def test_build_messages_includes_system_prompt(self):
        from unittest.mock import MagicMock
        agent = MagicMock()
        node = LLMNode(agent)

        ctx = AgentContext(
            system_prompt="You are helpful.",
            history=ListMessageHistory([
                {"role": "user", "content": "hello"},
            ]),
            tool_manager=InMemoryToolManager(),
        )

        messages = await node._build_messages(ctx)
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are helpful."
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_empty_system_prompt_omitted(self):
        from unittest.mock import MagicMock
        agent = MagicMock()
        node = LLMNode(agent)

        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([
                {"role": "user", "content": "hello"},
            ]),
            tool_manager=InMemoryToolManager(),
        )

        messages = await node._build_messages(ctx)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"


# ═══════════════════════════════════════════════════════════════════════════════
# 11. InlineWait strategy
# ═══════════════════════════════════════════════════════════════════════════════

class TestInlineWaitStrategy:
    """Verify InlineWait strategy correctly polls and returns decisions."""

    @pytest.mark.asyncio
    async def test_all_allowed(self):
        class _MockChannel:
            def __init__(self, responses):
                self._responses = responses
                self._idx = 0

            async def wait_for_decision(self, tool_call_id):
                resp = self._responses[self._idx]
                self._idx += 1
                return resp

        class _MockEmitter:
            async def emit(self, event, data=None):
                pass

        strategy = InlineWaitStrategy(_MockChannel(["allowed", "allowed"]))
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        ctx = type("Ctx", (), {"session_id": "s1", "emitter": _MockEmitter()})()
        decisions = await strategy.solicit_approval(reqs, ctx)
        assert decisions == [ApprovalDecision.ALLOWED, ApprovalDecision.ALLOWED]

    @pytest.mark.asyncio
    async def test_denied_cascades(self):
        class _MockChannel:
            async def wait_for_decision(self, tool_call_id):
                return "denied"

        class _MockEmitter:
            async def emit(self, event, data=None):
                pass

        strategy = InlineWaitStrategy(_MockChannel())
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        ctx = type("Ctx", (), {"session_id": "s1", "emitter": _MockEmitter()})()
        decisions = await strategy.solicit_approval(reqs, ctx)
        assert decisions == [ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED]

