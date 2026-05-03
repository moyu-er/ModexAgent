"""Verification tests for ReAct Graph refactoring — critical scenarios from spec & code review.

Tests the full approval flow, suspend-resume lifecycle, memory isolation,
event contract, engine routing, and bot_project wiring.
"""

import pytest
from framework.interceptor.builtin.tool_approval import (
    ToolNameMatcher,
)
from framework.approval.constants import ApprovalDecision, ApprovalTier, ApprovalStatus
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.approval.store import InMemoryApprovalStateStore
from framework.approval.response import parse_approval_action
from framework.approval.types import ApprovalAction
from framework.agents.react.state import (
    TurnResumeState, InMemoryTurnResumeStateStore,
)
from framework.agents.react.strategy import SuspendResumeStrategy, InlineWaitStrategy
from framework.agents.react.constants import ReActNode, ReActReason, ReActMetaKey
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
from framework.core.graph.interrupt import (
    GraphInterrupt, interrupt, _current_resume,
)
from framework.core.agent import AgentContext, ctx_ext

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

    def _make_classifier(self, *, dangerous_patterns=None, sensitive_patterns=None,
                          hardline_patterns=None):
        from framework.agents.react.approval import TieredToolApprovalClassifier
        dangerous = ToolNameMatcher(dangerous_patterns) if dangerous_patterns else None
        sensitive = ToolNameMatcher(sensitive_patterns) if sensitive_patterns else None
        hardline = ToolNameMatcher(hardline_patterns) if hardline_patterns else None
        return TieredToolApprovalClassifier(
            dangerous=dangerous,
            sensitive=sensitive,
            hardline=hardline,
        )

    def test_normal_tool_returns_normal(self):
        classifier = self._make_classifier(dangerous_patterns=["rm", "delete"])
        tc = ToolCall(tool_name="cat", call_id="c1", arguments={})
        ctx = AgentContext(system_prompt="test", history=ListMessageHistory(),
                            tool_manager=InMemoryToolManager())
        assert classifier.classify(tc, ctx) == ApprovalTier.NORMAL

    def test_dangerous_tool_returns_dangerous(self):
        classifier = self._make_classifier(dangerous_patterns=["rm", "delete", "rmdir"])
        tc = ToolCall(tool_name="rm", call_id="c1", arguments={"path": "/tmp/x"})
        ctx = AgentContext(system_prompt="test", history=ListMessageHistory(),
                            tool_manager=InMemoryToolManager())
        assert classifier.classify(tc, ctx) == ApprovalTier.DANGEROUS

    def test_sensitive_tool_returns_sensitive(self):
        classifier = self._make_classifier(sensitive_patterns=["read_file", "cat"])
        tc = ToolCall(tool_name="read_file", call_id="c1", arguments={"path": "/etc/passwd"})
        ctx = AgentContext(system_prompt="test", history=ListMessageHistory(),
                            tool_manager=InMemoryToolManager())
        assert classifier.classify(tc, ctx) == ApprovalTier.SENSITIVE

    def test_hardline_tool_returns_hardline(self):
        classifier = self._make_classifier(hardline_patterns=["sudo", "eval"])
        tc = ToolCall(tool_name="sudo", call_id="c1", arguments={"cmd": "rm -rf /"})
        ctx = AgentContext(system_prompt="test", history=ListMessageHistory(),
                            tool_manager=InMemoryToolManager())
        assert classifier.classify(tc, ctx) == ApprovalTier.HARDLINE

    def test_hardline_takes_priority_over_dangerous(self):
        classifier = self._make_classifier(
            dangerous_patterns=["sudo", "eval"],
            hardline_patterns=["sudo"],
        )
        tc = ToolCall(tool_name="sudo", call_id="c1", arguments={})
        ctx = AgentContext(system_prompt="test", history=ListMessageHistory(),
                            tool_manager=InMemoryToolManager())
        assert classifier.classify(tc, ctx) == ApprovalTier.HARDLINE

    def test_no_matchers_all_normal(self):
        classifier = self._make_classifier()
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
            ApprovalDecision.ALLOWED,
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
# 4. SuspendResume lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestSuspendResumeLifecycle:
    """Verify the full suspend-resume lifecycle: raise → save → restore → return."""

    def _make_ctx(self, session_id="s1", iteration=1, llm_response=None, iteration_msgs=None):
        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(), session_id=session_id,
        )
        ctx.metadata[ReActMetaKey.ITERATION] = iteration
        ctx.metadata[ReActMetaKey.ITERATION_MSGS] = iteration_msgs or []
        if llm_response is not None:
            ctx.metadata[ReActMetaKey.LLM_RESPONSE] = llm_response
        return ctx

    @pytest.mark.asyncio
    async def test_first_call_raises_graphinterrupt(self):
        strategy = SuspendResumeStrategy(
            InMemoryApprovalStateStore(), InMemoryTurnResumeStateStore(),
        )
        tc = ToolCall(tool_name="rm", call_id="c1", arguments={"path": "/tmp/x"})
        ctx = self._make_ctx(llm_response=LLMResponse(
            content="I'll delete that", tool_calls=[tc], finish_reason="tool_calls",
        ))
        req = ApprovalRequest("rm", "c1", {"path": "/tmp/x"}, "dangerous", 1)

        with pytest.raises(GraphInterrupt) as exc_info:
            await strategy.solicit_approval([req], ctx)
        assert exc_info.value.value == [req]

    @pytest.mark.asyncio
    async def test_second_call_returns_decisions(self):
        approval_store = InMemoryApprovalStateStore()
        strategy = SuspendResumeStrategy(approval_store, InMemoryTurnResumeStateStore())
        tc = ToolCall(tool_name="rm", call_id="c1", arguments={"path": "/tmp/x"})
        ctx = self._make_ctx(llm_response=LLMResponse(
            content="I'll delete", tool_calls=[tc], finish_reason="tool_calls",
        ))
        req = ApprovalRequest("rm", "c1", {"path": "/tmp/x"}, "dangerous", 1)

        # First call raises
        with pytest.raises(GraphInterrupt):
            await strategy.solicit_approval([req], ctx)

        # Approval state persisted
        saved = await approval_store.load("s1")
        assert saved is not None

        # Resume with injected decisions
        token = _current_resume.set([ApprovalDecision.ALLOWED])
        try:
            decisions = await strategy.solicit_approval([req], ctx)
            assert decisions == [ApprovalDecision.ALLOWED]
        finally:
            _current_resume.reset(token)

    @pytest.mark.asyncio
    async def test_turn_resume_state_persists_llm_response_fields(self):
        resume_store = InMemoryTurnResumeStateStore()
        strategy = SuspendResumeStrategy(
            InMemoryApprovalStateStore(), resume_store,
        )
        tcs = [
            ToolCall(tool_name="cat", call_id="c1", arguments={}),
            ToolCall(tool_name="rm", call_id="c2", arguments={"path": "/x"}),
        ]
        ctx = self._make_ctx(llm_response=LLMResponse(
            content="Let me check and clean up",
            reasoning_content="Need to read first then delete",
            tool_calls=list(tcs), finish_reason="tool_calls",
        ))
        req = ApprovalRequest("rm", "c2", {"path": "/x"}, "dangerous", 1)

        all_tc = [
            {"id": "c1", "type": "function", "function": {"name": "cat", "arguments": {}}},
            {"id": "c2", "type": "function", "function": {"name": "rm", "arguments": {"path": "/x"}}},
        ]
        with pytest.raises(GraphInterrupt):
            await strategy.solicit_approval(
                [req], ctx, all_tc, "Let me check and clean up",
                "Need to read first then delete",
            )

        resume_state = await resume_store.load("s1")
        assert resume_state is not None
        assert resume_state.llm_content == "Let me check and clean up"
        assert resume_state.llm_reasoning == "Need to read first then delete"
        # ALL tool calls should be saved (not just PENDING)
        assert len(resume_state.tool_calls) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 5. StartNode resume routing
# ═══════════════════════════════════════════════════════════════════════════════

class TestStartNodeResume:
    """Verify StartNode correctly reconstructs LLM_RESPONSE and routes to ToolNode on resume."""

    @pytest.mark.asyncio
    async def test_resume_reconstructs_llm_response(self):
        node = StartNode()
        resume = TurnResumeState(
            iteration=3,
            tool_calls=[
                {"id": "c1", "type": "function",
                 "function": {"name": "cat", "arguments": {}}},
                {"id": "c2", "type": "function",
                 "function": {"name": "rm", "arguments": {"path": "/tmp/x"}}},
            ],
            tool_decisions=["allowed", "pending"],
            all_new_messages=[{"role": "assistant", "content": "Let me check..."}],
            llm_content="Let me check...",
            llm_reasoning=None,
        )

        class _MockEmitter:
            async def emit(self, event, data=None):
                pass

        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={ReActMetaKey.RESUME_STATE: resume},
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.TOOL
        assert t.reason == ReActReason.RESUME_TOOLS
        assert ctx.metadata[ReActMetaKey.ITERATION] == 3
        assert ctx.metadata[ReActMetaKey.TOOL_DECISIONS] == ["allowed", "pending"]

        # LLM_RESPONSE should be reconstructed
        llm_resp = ctx.metadata[ReActMetaKey.LLM_RESPONSE]
        assert llm_resp is not None
        assert llm_resp.content == "Let me check..."
        assert len(llm_resp.tool_calls) == 2
        assert llm_resp.tool_calls[0].tool_name == "cat"
        assert llm_resp.tool_calls[1].tool_name == "rm"

    @pytest.mark.asyncio
    async def test_normal_start_routes_to_llm(self):
        node = StartNode()

        class _MockEmitter:
            async def emit(self, event, data=None):
                pass

        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )
        ctx.emitter = _MockEmitter()

        t = await node.execute(ctx)
        assert t.target == ReActNode.LLM
        assert t.reason == ReActReason.NORMAL_START
        assert ctx.metadata[ReActMetaKey.ITERATION] == 0


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
# 7. Event Contract: ITERATION_START/END
# ═══════════════════════════════════════════════════════════════════════════════

class TestIterationEvents:
    """Verify ITERATION_START and ITERATION_END are emitted."""

    @pytest.mark.asyncio
    async def test_tool_node_emits_iteration_end(self):
        from unittest.mock import MagicMock

        class _MockAgent:
            async def _execute_tool(self, tc, ctx):
                return ToolResult(tool_name=tc.tool_name, result=f"ok_{tc.tool_name}")
            def _build_tool_message(self, result, call_id):
                return {"role": "tool", "content": str(result.result)}
            async def _call_hooks(self, *a, **kw):
                pass
            async def _drain_injections(self, ctx):
                return []
            async def _save_checkpoint(self, msgs, ctx):
                pass

        class _Emitter:
            def __init__(self):
                self.events = []
            async def emit(self, event, data=None):
                self.events.append((event, data))

        agent = _MockAgent()
        node = ToolNode(agent)
        emitter = _Emitter()

        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
            },
        )
        ctx.emitter = emitter

        tc = ToolCall(tool_name="search", call_id="c1", arguments={})
        t = await node._execute_batch([tc], [ApprovalDecision.ALLOWED], ctx)

        assert t.target == ReActNode.LLM
        # Verify ITERATION_END was emitted
        end_events = [e for e in emitter.events if e[0] == ReActEvent.ITERATION_END]
        assert len(end_events) == 1
        assert end_events[0][1]["has_tool_calls"] is True


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


# ═══════════════════════════════════════════════════════════════════════════════
# 12. ToolNode batch execute: denied writes error result
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolNodeBatchExecute:
    """Verify ToolNode._execute_batch correctly handles denied tools with pseudo results."""

    @pytest.mark.asyncio
    async def test_denied_tool_result_contains_error(self):
        class _MockAgent:
            async def _execute_tool(self, tc, ctx):
                return ToolResult(tool_name="t1", result="ok")
            def _build_tool_message(self, result, call_id):
                return {
                    "role": "tool", "tool_call_id": call_id or "x",
                    "name": result.tool_name,
                    "content": str(result.result) if result.result else str(result.error),
                }
            async def _call_hooks(self, *a, **kw):
                pass
            async def _drain_injections(self, ctx):
                return []
            async def _save_denial_checkpoint(self, all_messages, ctx):
                pass
            async def _save_checkpoint(self, msgs, ctx):
                pass

        agent = _MockAgent()

        node = ToolNode(agent)

        class _Emitter:
            def __init__(self):
                self.events = []
            async def emit(self, event, data=None):
                self.events.append((event, data))

        class _History:
            def __init__(self):
                self.msgs = []
            async def append(self, msg):
                self.msgs.append(msg)

        emitter = _Emitter()
        history = _History()
        tc1 = ToolCall(tool_name="t1", call_id="c1", arguments={})
        tc2 = ToolCall(tool_name="t2", call_id="c2", arguments={})

        ctx = AgentContext(
            system_prompt="test", history=history,
            tool_manager=InMemoryToolManager(),
            metadata={
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: True,
            },
        )
        ctx.emitter = emitter

        t = await node._execute_batch(
            [tc1, tc2],
            [ApprovalDecision.ALLOWED, ApprovalDecision.DENIED],
            ctx,
        )
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.TURN_CANCELLED
        # Two tool messages should be written
        assert len(history.msgs) == 2
        # Second tool (denied) should have error content
        assert "Error" in str(history.msgs[1].get("content", ""))


# ═══════════════════════════════════════════════════════════════════════════════
# 13. EndNode writes result to metadata
# ═══════════════════════════════════════════════════════════════════════════════

class TestEndNodeResult:
    """Verify EndNode writes AgentResult to ctx.metadata[GraphMetaKey.GRAPH_RESULT]."""

    @pytest.mark.asyncio
    async def test_writes_result_to_metadata(self):
        class _MockAgent:
            async def _clear_checkpoint(self, ctx):
                pass
        agent = _MockAgent()
        node = EndNode(agent)

        llm_resp = LLMResponse(content="Done!", tool_calls=None, finish_reason="stop")

        class _Emitter:
            def __init__(self):
                self.events = []
            async def emit(self, event, data=None):
                self.events.append((event, data))
            async def emit_complete(self, result):
                self.events.append(("complete", result))

        emitter = _Emitter()
        ctx = AgentContext(
            system_prompt="test", history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            metadata={
                ReActMetaKey.LLM_RESPONSE: llm_resp,
                ReActMetaKey.ITERATION_MSGS: [],
            },
        )
        ctx.emitter = emitter

        t = await node.execute(ctx)
        assert t.target == GraphNode.END
        result = ctx.metadata[GraphMetaKey.GRAPH_RESULT]
        assert result.content == "Done!"
