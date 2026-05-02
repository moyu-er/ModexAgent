"""E2E tests for the full approval flow in bot_project context.

Tests:
1. Main agent path-based approval: write within allowed dir -> no approval
2. Main agent path-based approval: write outside allowed dir -> approval required
3. Peer agent: no approval interceptor (all paths allowed)
4. Full approve/resume lifecycle for main agent
5. Deny cascade for main agent
6. ArgumentMatcher edge cases
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.agents.react.constants import ReActMetaKey, ReActNode, ReActReason
from framework.agents.react.nodes.start import StartNode
from framework.agents.react.nodes.tool import ToolNode
from framework.agents.react.state import (
    InMemoryTurnResumeStateStore,
    TurnResumeState,
)
from framework.agents.react.strategy import SuspendResumeStrategy
from framework.approval.constants import ApprovalDecision, ApprovalTier
from framework.approval.state import ApprovalRequest
from framework.approval.store import InMemoryApprovalStateStore
from framework.core.agent import AgentContext
from framework.core.context_extensions import ExtensionKey
from framework.core.emitter import ToolCall
from framework.core.graph.interrupt import GraphInterrupt, _current_resume
from framework.core.tool_manager import InMemoryToolManager, ToolResult
from framework.core.types import LLMResponse
from framework.interceptor.builtin.tool_approval import (
    ArgumentMatcher,
    TieredToolApprovalInterceptor,
    ToolNameMatcher,
)
from framework.interceptor.chain import InterceptorChain
from framework.memory.history import ListMessageHistory


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_ctx_with_llm(
    *,
    session_id="s1",
    tool_calls,
    llm_content="test content",
    interceptor_chain=None,
    suspend_strategy=None,
    extra_metadata=None,
):
    """Build an AgentContext with LLM_RESPONSE preset for ToolNode execution."""
    tcs = list(tool_calls)
    llm_resp = LLMResponse(
        content=llm_content, tool_calls=tcs, finish_reason="tool_calls",
    )
    ctx = AgentContext(
        system_prompt="test", history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session_id=session_id,
        metadata={
            ReActMetaKey.LLM_RESPONSE: llm_resp,
            ReActMetaKey.ITERATION: 1,
            ReActMetaKey.ITERATION_MSGS: [],
            **(extra_metadata or {}),
        },
    )
    if interceptor_chain is not None:
        ctx.extensions[ExtensionKey.INTERCEPTOR_CHAIN] = interceptor_chain
    if suspend_strategy is not None:
        ctx.extensions[ExtensionKey.SUSPEND_STRATEGY] = suspend_strategy
    return ctx


class _CountingHistory(ListMessageHistory):
    """Tracks appended messages for assertions."""
    def __init__(self):
        super().__init__()
        self.msgs = []

    async def append(self, msg):
        self.msgs.append(msg)
        await super().append(msg)


def _make_mock_agent():
    """Create a mock ReActAgent for ToolNode testing.

    Async methods use AsyncMock; sync methods use MagicMock.
    """
    agent = MagicMock()
    # Async methods (called with await)
    agent._execute_tool = AsyncMock(return_value=ToolResult(
        tool_name="mock_tool", call_id="c1", result="ok",
    ))
    agent._call_hooks = AsyncMock()
    agent._drain_injections = AsyncMock(return_value=[])
    agent._save_checkpoint = AsyncMock()
    agent._save_denial_checkpoint = AsyncMock()
    # Sync methods
    agent._build_tool_message = MagicMock(return_value={
        "role": "tool", "tool_call_id": "c1", "name": "mock_tool", "content": "ok",
    })
    return agent


# ── Test Classes ─────────────────────────────────────────────────────────


class TestArgumentMatcher:
    """Verify ArgumentMatcher correctly checks paths against allowed directories."""

    def test_path_within_allowed(self):
        matcher = ArgumentMatcher({"/tmp/allowed"}, workspace="/tmp/allowed")
        tc = ToolCall(
            tool_name="write_file", call_id="c1",
            arguments={"path": "/tmp/allowed/subdir/file.txt"},
        )
        assert matcher.is_allowed(tc) is True

    def test_path_outside_allowed(self):
        matcher = ArgumentMatcher({"/tmp/allowed"}, workspace="/tmp/allowed")
        tc = ToolCall(
            tool_name="write_file", call_id="c1",
            arguments={"path": "/etc/hosts"},
        )
        assert matcher.is_allowed(tc) is False

    def test_no_path_argument(self):
        matcher = ArgumentMatcher({"/tmp/allowed"}, workspace="/tmp/allowed")
        tc = ToolCall(
            tool_name="write_file", call_id="c1",
            arguments={"content": "hello"},
        )
        assert matcher.is_allowed(tc) is True

    def test_relative_path_resolved(self):
        matcher = ArgumentMatcher({"."}, workspace="/Users/user/project")
        tc = ToolCall(
            tool_name="write_file", call_id="c1",
            arguments={"file_path": "./data/output.txt"},
        )
        assert matcher.is_allowed(tc) is True

    def test_multiple_allowed_dirs(self):
        matcher = ArgumentMatcher({"/tmp/a", "/tmp/b"}, workspace="/tmp/a")
        tc = ToolCall(
            tool_name="write_file", call_id="c1",
            arguments={"path": "/tmp/b/file.txt"},
        )
        assert matcher.is_allowed(tc) is True


class TestPathBasedApprovalClassification:
    """Verify TieredToolApprovalInterceptor.classify_tier with ArgumentMatcher."""

    def _make_interceptor(self, dangerous_tools, allowed_dirs=None):
        dangerous = ToolNameMatcher(set(dangerous_tools)) if dangerous_tools else None
        arg_matcher = ArgumentMatcher(set(allowed_dirs)) if allowed_dirs else None
        return TieredToolApprovalInterceptor(
            channel=MagicMock(),
            dangerous_matcher=dangerous,
            argument_matcher=arg_matcher,
        )

    def test_dangerous_tool_outside_allowed_dir_returns_dangerous(self):
        interceptor = self._make_interceptor(
            dangerous_tools=["write_file", "shell"],
            allowed_dirs=["/safe"],
        )
        tc = ToolCall(
            tool_name="write_file", call_id="c1",
            arguments={"path": "/etc/passwd"},
        )
        assert interceptor.classify_tier(tc) == ApprovalTier.DANGEROUS

    def test_dangerous_tool_within_allowed_dir_returns_normal(self):
        interceptor = self._make_interceptor(
            dangerous_tools=["write_file", "shell"],
            allowed_dirs=["/safe"],
        )
        tc = ToolCall(
            tool_name="write_file", call_id="c1",
            arguments={"path": "/safe/data.txt"},
        )
        assert interceptor.classify_tier(tc) == ApprovalTier.NORMAL

    def test_normal_tool_always_normal(self):
        interceptor = self._make_interceptor(
            dangerous_tools=["write_file", "shell"],
            allowed_dirs=["/safe"],
        )
        tc = ToolCall(
            tool_name="cat", call_id="c1",
            arguments={"path": "/etc/passwd"},
        )
        assert interceptor.classify_tier(tc) == ApprovalTier.NORMAL

    def test_dangerous_tool_no_argument_matcher_returns_dangerous(self):
        """Without argument matcher, all dangerous tools require approval."""
        interceptor = self._make_interceptor(
            dangerous_tools=["write_file"],
            allowed_dirs=None,
        )
        tc = ToolCall(
            tool_name="write_file", call_id="c1",
            arguments={"path": "/safe/data.txt"},
        )
        assert interceptor.classify_tier(tc) == ApprovalTier.DANGEROUS


class TestToolNodePathBasedClassification:
    """Verify ToolNode._classify_all works with path-based approval."""

    def test_mixed_path_classification(self):
        """Main agent: safe write ALLOWED, dangerous write PENDING, normal tool ALLOWED."""
        interceptor = TieredToolApprovalInterceptor(
            channel=MagicMock(),
            dangerous_matcher=ToolNameMatcher({"write_file", "rm"}),
            argument_matcher=ArgumentMatcher({"/safe"}),
        )
        chain = InterceptorChain(interceptors=[interceptor])

        node = ToolNode(_make_mock_agent(), enable_approval=True, enable_hooks=False)
        ctx = _make_ctx_with_llm(
            tool_calls=[
                ToolCall(tool_name="write_file", call_id="c1",
                         arguments={"path": "/safe/data.txt"}),    # safe -> ALLOWED
                ToolCall(tool_name="write_file", call_id="c2",
                         arguments={"path": "/etc/hosts"}),        # dangerous -> PENDING
                ToolCall(tool_name="cat", call_id="c3",
                         arguments={"path": "/any/file.txt"}),     # normal -> ALLOWED
            ],
            interceptor_chain=chain,
        )
        decisions = node._classify_all(
            ctx.metadata[ReActMetaKey.LLM_RESPONSE].tool_calls, ctx,
        )
        assert decisions == [
            ApprovalDecision.ALLOWED,
            ApprovalDecision.PENDING,
            ApprovalDecision.ALLOWED,
        ]


class TestMainVsPeerInterceptorSeparation:
    """Verify main agent gets approval chain, peers do not."""

    def test_main_chain_has_approval_interceptor(self):
        """Simulate bot_project's main_interceptor_chain construction."""
        base_chain = InterceptorChain()
        base_chain.add(MagicMock())  # base interceptor e.g., tool_timeout

        main_chain = InterceptorChain()
        for i in base_chain.interceptors:
            main_chain.add(i)
        main_chain.add(
            TieredToolApprovalInterceptor(
                channel=MagicMock(),
                dangerous_matcher=ToolNameMatcher({"shell", "write_file"}),
                argument_matcher=ArgumentMatcher({"."}),
            )
        )

        assert len(main_chain.interceptors) == 2
        assert len(base_chain.interceptors) == 1

        node = ToolNode(_make_mock_agent(), enable_approval=True, enable_hooks=False)
        ctx = _make_ctx_with_llm(
            tool_calls=[
                ToolCall(tool_name="write_file", call_id="c1",
                         arguments={"path": "/etc/hosts"}),
            ],
            interceptor_chain=main_chain,
        )
        decisions = node._classify_all(
            ctx.metadata[ReActMetaKey.LLM_RESPONSE].tool_calls, ctx,
        )
        assert decisions == [ApprovalDecision.PENDING]  # Outside allowed dir (.)

    def test_peer_chain_no_approval(self):
        """Peer agents use base chain without approval - no classify_tier -> ALLOWED."""
        base_chain = InterceptorChain()
        # Use a plain object (not MagicMock) so getattr won't auto-create classify_tier
        class _NoApprovalInterceptor:
            pass
        base_chain.add(_NoApprovalInterceptor())

        node = ToolNode(_make_mock_agent(), enable_approval=True, enable_hooks=False)
        ctx = _make_ctx_with_llm(
            tool_calls=[
                ToolCall(tool_name="write_file", call_id="c1",
                         arguments={"path": "/etc/hosts"}),
            ],
            interceptor_chain=base_chain,
        )
        decisions = node._classify_all(
            ctx.metadata[ReActMetaKey.LLM_RESPONSE].tool_calls, ctx,
        )
        assert all(d == ApprovalDecision.ALLOWED for d in decisions)


class TestFullApprovalResumeFlow:
    """E2E test: main agent tool approval -> suspend -> resume -> execute."""

    @pytest.mark.asyncio
    async def test_approve_resume_execute(self):
        """Full flow: strategy suspends, user approves, ToolNode resumes and executes."""
        interceptor = TieredToolApprovalInterceptor(
            channel=MagicMock(),
            dangerous_matcher=ToolNameMatcher({"write_file"}),
            argument_matcher=ArgumentMatcher({"/safe"}),
        )
        chain = InterceptorChain(interceptors=[interceptor])

        approval_store = InMemoryApprovalStateStore()
        resume_store = InMemoryTurnResumeStateStore()
        strategy = SuspendResumeStrategy(approval_store, resume_store)

        tc_dangerous = ToolCall(
            tool_name="write_file", call_id="c1",
            arguments={"path": "/etc/hosts"},
        )

        # Phase 1: First solicit -> raises GraphInterrupt, persists state
        ctx1 = _make_ctx_with_llm(
            session_id="s1",
            tool_calls=[tc_dangerous],
            llm_content="Let me write to that file",
        )
        req = ApprovalRequest(
            tool_name="write_file", tool_call_id="c1",
            arguments={"path": "/etc/hosts"}, tier="dangerous", iteration=1,
        )
        with pytest.raises(GraphInterrupt) as exc:
            await strategy.solicit_approval([req], ctx1)
        assert exc.value.value is not None

        # Phase 2: Approval state persisted
        saved_approval = await approval_store.load("s1")
        assert saved_approval is not None
        assert saved_approval.unresolved_count == 1

        # Phase 3: User approves
        saved_approval.apply("c1", ApprovalDecision.ALLOWED)
        assert saved_approval.every_tool_decided

        # Phase 4: Resume - load persisted state
        resume_state = await resume_store.load("s1")
        assert resume_state is not None

        # Phase 5: Build resume context, simulate pipeline's RESUME_STATE injection
        history = _CountingHistory()
        ctx2 = AgentContext(
            system_prompt="test", history=history,
            tool_manager=InMemoryToolManager(), session_id="s1",
            metadata={
                ReActMetaKey.RESUME_STATE: resume_state,
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
            },
        )
        ctx2.extensions[ExtensionKey.SUSPEND_STRATEGY] = strategy
        ctx2.extensions[ExtensionKey.INTERCEPTOR_CHAIN] = chain

        # Phase 6: StartNode reconstructs LLM_RESPONSE and routes to ToolNode
        t = await StartNode().execute(ctx2)
        assert t.target == ReActNode.TOOL
        assert t.reason == ReActReason.RESUME_TOOLS
        assert ReActMetaKey.LLM_RESPONSE in ctx2.metadata

        # Phase 7: ToolNode executes with injected decisions
        agent = _make_mock_agent()
        node = ToolNode(agent, enable_approval=True, enable_hooks=False)
        _current_resume.set([ApprovalDecision.ALLOWED])
        try:
            t = await node.execute(ctx2)
            assert t.target == ReActNode.LLM
            assert t.reason == ReActReason.TOOLS_DONE
            assert len(history.msgs) >= 1
        finally:
            _current_resume.set(None)

    @pytest.mark.asyncio
    async def test_deny_cascade_flow(self):
        """Multiple tools: deny first -> rest preempted -> turn cancelled."""
        interceptor = TieredToolApprovalInterceptor(
            channel=MagicMock(),
            dangerous_matcher=ToolNameMatcher({"write_file", "shell"}),
            argument_matcher=ArgumentMatcher({"/safe"}),
        )
        chain = InterceptorChain(interceptors=[interceptor])

        approval_store = InMemoryApprovalStateStore()
        resume_store = InMemoryTurnResumeStateStore()
        strategy = SuspendResumeStrategy(approval_store, resume_store)

        tc1 = ToolCall(tool_name="write_file", call_id="c1", arguments={"path": "/etc/a"})
        tc2 = ToolCall(tool_name="write_file", call_id="c2", arguments={"path": "/etc/b"})

        # Phase 1: First solicit -> raises GraphInterrupt
        ctx1 = _make_ctx_with_llm(
            session_id="s2",
            tool_calls=[tc1, tc2],
            llm_content="Writing files",
            extra_metadata={ReActMetaKey.DENY_AS_CANCEL: True},
        )
        reqs = [
            ApprovalRequest(tc1.tool_name, tc1.call_id or "c1", tc1.arguments, "dangerous", 1),
            ApprovalRequest(tc2.tool_name, tc2.call_id or "c2", tc2.arguments, "dangerous", 1),
        ]
        with pytest.raises(GraphInterrupt):
            await strategy.solicit_approval(reqs, ctx1)

        # Phase 2: State persisted
        saved = await approval_store.load("s2")
        assert saved is not None
        assert saved.unresolved_count == 2

        # Phase 3: User denies first tool
        saved.apply("c1", ApprovalDecision.DENIED)
        assert saved.every_tool_decided
        assert saved.final_decisions() == [
            ApprovalDecision.DENIED,
            ApprovalDecision.PREEMPTED,
        ]

        # Phase 4: Resume - load state
        resume_state = await resume_store.load("s2")
        assert resume_state is not None

        # Phase 5: Rebuild context with DENY_AS_CANCEL flag
        history = _CountingHistory()
        ctx2 = AgentContext(
            system_prompt="test", history=history,
            tool_manager=InMemoryToolManager(), session_id="s2",
            metadata={
                ReActMetaKey.RESUME_STATE: resume_state,
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: True,
            },
        )
        ctx2.extensions[ExtensionKey.SUSPEND_STRATEGY] = strategy
        ctx2.extensions[ExtensionKey.INTERCEPTOR_CHAIN] = chain

        # Phase 6: StartNode -> ToolNode
        t = await StartNode().execute(ctx2)
        assert t.target == ReActNode.TOOL

        # Phase 7: ToolNode with deny -> cancels turn
        agent = _make_mock_agent()
        node = ToolNode(agent, enable_approval=True, enable_hooks=False)
        _current_resume.set(saved.final_decisions())
        try:
            t = await node.execute(ctx2)
            assert t.target == ReActNode.END
            assert t.reason == ReActReason.TURN_CANCELLED
        finally:
            _current_resume.set(None)
