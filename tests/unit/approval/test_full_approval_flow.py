"""TDD integration tests for full approval flow — state, approve/deny, resume, execute, cleanup.

Covers the complete lifecycle:
- State save on interrupt, refresh on partial, read on full approval
- Approval trigger via classify_tier
- Approve → resume → ToolNode executes → LLM continues
- Deny → pseudo tool result → turn cancelled
- State cleanup after completion
- Memory correctness (no duplicates)
- Cross-turn isolation (_current_resume cleared)
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.agents.react.agent import ReActAgent, ReActEvent
from framework.agents.react.graph import ReActGraph
from framework.agents.react.nodes.start import StartNode
from framework.agents.react.nodes.llm import LLMNode
from framework.agents.react.nodes.tool import ToolNode
from framework.agents.react.nodes.end import EndNode
from framework.agents.react.strategy import SuspendResumeStrategy
from framework.agents.react.state import (
    TurnResumeState, InMemoryTurnResumeStateStore, StateStoreTurnResumeStateStore,
)
from framework.agents.react.constants import ReActNode, ReActReason, ReActMetaKey
from framework.approval.constants import ApprovalDecision, ApprovalTier
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.approval.store import (
    InMemoryApprovalStateStore, LocalFileApprovalStateStore,
)
from framework.core.agent import AgentContext, ctx_ext
from framework.core.context_extensions import ExtensionKey
from framework.core.emitter import AgentResult, ToolCall, ToolResult
from framework.core.types import LLMResponse
from framework.core.constants import FinishReason
from framework.core.tool_manager import InMemoryToolManager
from framework.core.graph.engine import GraphEngine
from framework.core.graph.interrupt import GraphInterrupt, _current_resume, interrupt
from framework.core.graph.constants import GraphMetaKey
from framework.hook import HookPoint
from framework.interceptor.chain import InterceptorChain
from framework.interceptor.builtin.tool_approval import (
    TieredToolApprovalInterceptor, ToolNameMatcher, ArgumentMatcher,
)
from framework.memory.history import ListMessageHistory


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

class _MockEmitter:
    def __init__(self):
        self.events = []
        self.completed = None

    async def emit(self, event, data=None):
        self.events.append((event, data))

    async def emit_complete(self, result):
        self.completed = result

    async def emit_delta(self, delta):
        pass

    async def emit_content(self, content):
        pass

    async def emit_stream_end(self, resuming=False):
        pass

    def wants_streaming(self):
        return False


class _TrackingHistory(ListMessageHistory):
    def __init__(self):
        super().__init__()
        self.appended = []

    async def append(self, msg):
        self.appended.append(msg)
        await super().append(msg)


def _make_approval_interceptor(dangerous_tools=None, allowed_dirs=None):
    dangerous = ToolNameMatcher(set(dangerous_tools)) if dangerous_tools else None
    arg_matcher = ArgumentMatcher(set(allowed_dirs)) if allowed_dirs else None
    return TieredToolApprovalInterceptor(
        channel=MagicMock(),
        dangerous_matcher=dangerous,
        argument_matcher=arg_matcher,
    )


def _make_ctx(*, session_id="s1", history=None, interceptor_chain=None,
              suspend_strategy=None, metadata=None):
    ctx = AgentContext(
        system_prompt="You are helpful.",
        history=history if history is not None else _TrackingHistory(),
        tool_manager=InMemoryToolManager(),
        session_id=session_id,
        metadata=metadata or {},
    )
    ctx.emitter = _MockEmitter()
    if interceptor_chain:
        ctx.extensions[ExtensionKey.INTERCEPTOR_CHAIN] = interceptor_chain
    if suspend_strategy:
        ctx.extensions[ExtensionKey.SUSPEND_STRATEGY] = suspend_strategy
    return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# 1. State persistence: save/refresh/read
# ═══════════════════════════════════════════════════════════════════════════════

class TestApprovalStatePersistence:
    """State save/refresh/read lifecycle for approval data."""

    @pytest.mark.asyncio
    async def test_approval_state_saved_on_interrupt(self):
        """SuspendResumeStrategy saves ApprovalState when interrupt raises."""
        approval_store = InMemoryApprovalStateStore()
        resume_store = InMemoryTurnResumeStateStore()
        strategy = SuspendResumeStrategy(approval_store, resume_store)

        tc = ToolCall(tool_name="rm", call_id="c1", arguments={"path": "/etc/hosts"})
        ctx = _make_ctx(
            metadata={
                ReActMetaKey.LLM_RESPONSE: LLMResponse(
                    content="Deleting...", tool_calls=[tc], finish_reason="tool_calls",
                ),
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
            },
        )
        req = ApprovalRequest("rm", "c1", {"path": "/etc/hosts"}, "dangerous", 1)
        all_tc = [{"id": "c1", "type": "function", "function": {"name": "rm", "arguments": {"path": "/etc/hosts"}}}]

        with pytest.raises(GraphInterrupt):
            await strategy.solicit_approval(
                [req], ctx,
                all_tool_calls=all_tc,
                llm_content="Deleting...",
            )

        saved = await approval_store.load("s1")
        assert saved is not None
        assert saved.session_id == "s1"
        assert len(saved.requests) == 1
        assert saved.requests[0].tool_call_id == "c1"
        assert saved.status == "pending"
        # Resume state should also be saved
        resume = await resume_store.load("s1")
        assert resume is not None
        assert len(resume.tool_calls) == 1
        assert resume.tool_calls[0]["id"] == "c1"
        assert resume.llm_content == "Deleting..."

    @pytest.mark.asyncio
    async def test_approval_state_refreshed_on_partial_approval(self):
        """Pipeline simulates: first approve updates state, saves back."""
        store = InMemoryApprovalStateStore()
        # Pre-save state (simulating first pass)
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        state = ApprovalState(session_id="s1", requests=reqs)
        await store.save(state)

        # User approves first tool
        loaded = await store.load("s1")
        loaded.apply("c1", ApprovalDecision.ALLOWED)
        assert loaded.every_tool_decided is False
        assert loaded.status == "partial"
        await store.save(loaded)

        # Verify state reflected partial approval
        reloaded = await store.load("s1")
        assert reloaded.decisions == {"c1": ApprovalDecision.ALLOWED}
        assert reloaded.unresolved_count == 1

    @pytest.mark.asyncio
    async def test_approval_state_cleaned_after_full_approval(self):
        """After all tools decided and resume completes, state is deleted."""
        store = InMemoryApprovalStateStore()
        reqs = [ApprovalRequest("t1", "c1", {}, "dangerous", 1)]
        state = ApprovalState(session_id="s1", requests=reqs)
        await store.save(state)

        # Full approval + delete
        loaded = await store.load("s1")
        loaded.apply("c1", ApprovalDecision.ALLOWED)
        assert loaded.every_tool_decided is True
        await store.delete("s1")

        # Verify cleaned
        assert await store.load("s1") is None

    @pytest.mark.asyncio
    async def test_approval_state_file_persistence(self, tmp_path):
        """LocalFileApprovalStateStore correctly persists to disk."""
        store = LocalFileApprovalStateStore(tmp_path)
        reqs = [ApprovalRequest("rm", "c1", {"path": "/x"}, "dangerous", 1)]
        state = ApprovalState(session_id="s_test", requests=reqs)
        state.apply("c1", ApprovalDecision.ALLOWED)
        await store.save(state)

        # Verify file exists and is valid JSON
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        assert "s_test" in files[0].name

        # Load and verify
        loaded = await store.load("s_test")
        assert loaded is not None
        assert loaded.every_tool_decided is True
        assert loaded.decisions == {"c1": ApprovalDecision.ALLOWED}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Approval trigger via classify_tier
# ═══════════════════════════════════════════════════════════════════════════════

class TestApprovalTrigger:
    """ToolNode correctly triggers approval for dangerous tools."""

    def test_classify_all_returns_pending_for_dangerous(self):
        interceptor = _make_approval_interceptor(
            dangerous_tools=["rm", "write_file"],
            allowed_dirs={"/safe"},
        )
        chain = InterceptorChain(interceptors=[interceptor])
        ctx = _make_ctx(interceptor_chain=chain)

        agent = MagicMock()
        node = ToolNode(agent, enable_approval=True, enable_hooks=False)
        tcs = [
            ToolCall(tool_name="list_dir", call_id="c1", arguments={"path": "/safe"}),
            ToolCall(tool_name="rm", call_id="c2", arguments={"path": "/etc/hosts"}),
            ToolCall(tool_name="cat", call_id="c3", arguments={"path": "/safe/readme.txt"}),
        ]
        decisions = node._classify_all(tcs, ctx)
        # list_dir(safe) → NORMAL, rm(outside) → DANGEROUS, cat(safe, not dangerous) → NORMAL
        assert decisions == [
            ApprovalDecision.ALLOWED,
            ApprovalDecision.PENDING,
            ApprovalDecision.ALLOWED,
        ]

    @pytest.mark.asyncio
    async def test_execute_raises_graphinterrupt_on_pending(self):
        """When classification returns PENDING, strategy interrupt raises GraphInterrupt."""
        interceptor = _make_approval_interceptor(
            dangerous_tools=["rm"], allowed_dirs={"/safe"},
        )
        chain = InterceptorChain(interceptors=[interceptor])
        strategy = SuspendResumeStrategy(
            InMemoryApprovalStateStore(), InMemoryTurnResumeStateStore(),
        )
        ctx = _make_ctx(interceptor_chain=chain, suspend_strategy=strategy, metadata={
            ReActMetaKey.LLM_RESPONSE: LLMResponse(
                content="", tool_calls=[ToolCall(tool_name="rm", call_id="c1", arguments={"path": "/etc/hosts"})],
                finish_reason="tool_calls",
            ),
            ReActMetaKey.ITERATION: 1,
            ReActMetaKey.ITERATION_MSGS: [],
        })
        agent = MagicMock()
        node = ToolNode(agent, enable_approval=True, enable_hooks=False)

        with pytest.raises(GraphInterrupt) as exc:
            await node.execute(ctx)
        assert exc.value.value is not None
        assert len(exc.value.value) == 1  # one pending tool


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Full approve → resume → execute cycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestFullApproveResumeExecute:
    """Complete cycle: approve → state restore → ToolNode resume → tool executes → LLM continues."""

    @pytest.mark.asyncio
    async def test_resume_after_approval_executes_tool(self):
        """After approval, ToolNode resumes and executes the tool."""
        interceptor = _make_approval_interceptor(
            dangerous_tools=["rm"], allowed_dirs={"/safe"},
        )
        chain = InterceptorChain(interceptors=[interceptor])
        approval_store = InMemoryApprovalStateStore()
        resume_store = InMemoryTurnResumeStateStore()
        strategy = SuspendResumeStrategy(approval_store, resume_store)

        history = _TrackingHistory()
        executed = []

        class _MockAgent:
            async def _execute_tool(self, tc, ctx):
                executed.append(tc.tool_name)
                return ToolResult(tool_name=tc.tool_name, result=f"ok_{tc.tool_name}")
            def _build_tool_message(self, result, call_id):
                return {"role": "tool", "tool_call_id": call_id or "x",
                        "name": result.tool_name,
                        "content": str(result.result) if result.result else str(result.error)}
            async def _call_hooks(self, *a, **kw):
                pass
            async def _drain_injections(self, ctx):
                return []
            async def _save_checkpoint(self, msgs, ctx):
                pass
            async def _save_denial_checkpoint(self, ctx):
                pass

        agent = _MockAgent()

        # ── First pass: classify → interrupt ──
        tc = ToolCall(tool_name="rm", call_id="c1", arguments={"path": "/etc/hosts"})
        ctx1 = _make_ctx(
            history=history, interceptor_chain=chain, suspend_strategy=strategy,
            metadata={
                ReActMetaKey.LLM_RESPONSE: LLMResponse(
                    content="", tool_calls=[tc], finish_reason="tool_calls",
                ),
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [{"role": "assistant", "content": ""}],
            },
        )
        node1 = ToolNode(agent, enable_approval=True, enable_hooks=False)
        with pytest.raises(GraphInterrupt):
            await node1.execute(ctx1)

        # ── Pipeline simulates: user approves ──
        saved = await approval_store.load("s1")
        saved.apply("c1", ApprovalDecision.ALLOWED)
        assert saved.every_tool_decided
        decisions = saved.final_decisions()

        resume_state = await resume_store.load("s1")
        assert resume_state is not None

        # ── Resume: StartNode → ToolNode ──
        ctx2 = _make_ctx(
            history=history, interceptor_chain=chain, suspend_strategy=strategy,
            metadata={
                ReActMetaKey.RESUME_STATE: resume_state,
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
            },
        )
        # StartNode routes to tool
        start = StartNode()
        t = await start.execute(ctx2)
        assert t.target == ReActNode.TOOL
        assert t.reason == ReActReason.RESUME_TOOLS
        # LLM_RESPONSE reconstructed
        assert ReActMetaKey.LLM_RESPONSE in ctx2.metadata
        assert len(ctx2.metadata[ReActMetaKey.LLM_RESPONSE].tool_calls) == 1

        # ToolNode executes with decisions
        _current_resume.set(decisions)
        try:
            node2 = ToolNode(agent, enable_approval=True, enable_hooks=False)
            t = await node2.execute(ctx2)
            assert t.target == ReActNode.LLM
            assert t.reason == ReActReason.TOOLS_DONE
            assert len(executed) == 1
            assert executed[0] == "rm"
            # Tool result written to history
            assert len(history.appended) >= 1
            tool_msg = history.appended[-1]
            assert tool_msg["role"] == "tool"
        finally:
            _current_resume.set(None)

    @pytest.mark.asyncio
    async def test_full_graph_roundtrip_with_mock_llm(self):
        """End-to-end: LLM → tool_calls → classify → approve → resume → execute → LLM final answer."""
        interceptor = _make_approval_interceptor(
            dangerous_tools=["write_file"], allowed_dirs={"/safe"},
        )
        chain = InterceptorChain(interceptors=[interceptor])
        approval_store = InMemoryApprovalStateStore()
        resume_store = InMemoryTurnResumeStateStore()
        strategy = SuspendResumeStrategy(approval_store, resume_store)

        history = _TrackingHistory()
        executed = []

        # Mock LLM that returns tool_calls then final answer
        call_count = [0]

        class _MockProvider:
            async def chat(self, messages, tools=None, temperature=None, max_tokens=None):
                call_count[0] += 1
                if call_count[0] == 1:
                    return LLMResponse(
                        content="Let me write that.",
                        tool_calls=[ToolCall(tool_name="write_file", call_id="c1",
                                             arguments={"path": "/etc/hosts"})],
                        finish_reason="tool_calls",
                    )
                else:
                    return LLMResponse(
                        content="File written successfully!",
                        finish_reason="stop",
                    )

        agent = ReActAgent(_MockProvider(), mode="full")

        class _MockAgent:
            provider = _MockProvider()

            async def _execute_tool(self, tc, ctx):
                executed.append(tc.tool_name)
                return ToolResult(tool_name=tc.tool_name, result=f"ok_{tc.tool_name}")
            def _build_tool_message(self, result, call_id):
                return {"role": "tool", "tool_call_id": call_id or "x",
                        "name": result.tool_name,
                        "content": str(result.result) if result.result else str(result.error)}
            async def _call_hooks(self, *a, **kw):
                pass
            async def _drain_injections(self, ctx):
                return []
            async def _save_checkpoint(self, msgs, ctx):
                pass
            async def _save_denial_checkpoint(self, ctx):
                pass
            async def _clear_checkpoint(self, ctx):
                pass
            async def _stream_with_control(self, messages, ctx):
                return LLMResponse(content="ok", finish_reason="stop")
            def _build_assistant_message(self, content, tool_calls):
                msg = {"role": "assistant", "content": content or ""}
                if tool_calls:
                    msg["tool_calls"] = [
                        {"id": tc.call_id or "", "type": "function",
                         "function": {"name": tc.tool_name, "arguments": tc.arguments or {}}}
                        for tc in tool_calls
                    ]
                return msg

        mock_agent = _MockAgent()
        # Build graph with mock agent
        graph = ReActGraph(mock_agent, mode="full")
        engine = GraphEngine(graph)

        ctx = _make_ctx(
            session_id="s1", history=history,
            interceptor_chain=chain, suspend_strategy=strategy,
            metadata={ReActMetaKey.ITERATION: 0},
        )
        # Set checkpoint_store to None (not needed for this test)
        ctx.extensions[ExtensionKey.CHECKPOINT_STORE] = None

        # ── First run: LLM → interrupt ──
        with pytest.raises(GraphInterrupt) as exc:
            await engine.run(ctx)
        assert len(exc.value.value) == 1

        # Verify assistant message was written to history
        assistant_msgs = [m for m in history.appended if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1

        # ── Simulate user approve ──
        saved = await approval_store.load("s1")
        saved.apply("c1", ApprovalDecision.ALLOWED)
        decisions = saved.final_decisions()

        resume_state = await resume_store.load("s1")
        assert resume_state is not None
        assert len(resume_state.tool_calls) == 1

        # ── Second run: resume with decisions ──
        ctx2 = _make_ctx(
            session_id="s1", history=history,
            interceptor_chain=chain, suspend_strategy=strategy,
            metadata={
                ReActMetaKey.RESUME_STATE: resume_state,
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
            },
        )
        ctx2.extensions[ExtensionKey.CHECKPOINT_STORE] = None

        _current_resume.set(decisions)
        try:
            result = await engine.run(ctx2)
            assert result is not None
            assert result.content == "File written successfully!"
            assert len(executed) == 1
            assert executed[0] == "write_file"
        finally:
            _current_resume.set(None)

        # Verify ALL messages in history: assistant(tc) + tool + assistant(final)
        all_roles = [m["role"] for m in history.appended]
        assert all_roles.count("assistant") == 2  # first + final
        assert all_roles.count("tool") == 1

        # ── Cleanup ──
        await approval_store.delete("s1")
        await resume_store.delete("s1")
        assert await approval_store.load("s1") is None


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Deny flow → pseudo tool result → turn cancelled
# ═══════════════════════════════════════════════════════════════════════════════

class TestDenyFlow:
    """Denied tools produce pseudo error results and cancel the turn."""

    @pytest.mark.asyncio
    async def test_denied_tool_writes_error_to_memory(self):
        """DENIED tool → pseudo tool result with error text in history."""
        agent = MagicMock()
        agent._execute_tool = AsyncMock(return_value=ToolResult(tool_name="rm", result="ok"))
        agent._build_tool_message = MagicMock(return_value={
            "role": "tool", "tool_call_id": "c1",
            "name": "rm", "content": "Error: denied",
        })
        agent._call_hooks = AsyncMock()
        agent._drain_injections = AsyncMock(return_value=[])
        agent._save_checkpoint = AsyncMock()
        agent._save_denial_checkpoint = AsyncMock()

        node = ToolNode(agent, enable_approval=False, enable_hooks=False)
        history = _TrackingHistory()
        tc = ToolCall(tool_name="rm", call_id="c1", arguments={"path": "/etc/hosts"})
        ctx = _make_ctx(
            history=history,
            metadata={
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: True,
            },
        )

        t = await node._execute_batch([tc], [ApprovalDecision.DENIED], ctx)
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.TURN_CANCELLED
        # Error tool message written
        assert len(history.appended) >= 1

    @pytest.mark.asyncio
    async def test_cascade_preempted(self):
        """First tool denied → all subsequent tools PREEMPTED."""
        agent = MagicMock()
        agent._execute_tool = AsyncMock(return_value=ToolResult(tool_name="t1", result="ok"))
        agent._build_tool_message = MagicMock(side_effect=lambda r, cid: {
            "role": "tool", "tool_call_id": cid or "x",
            "name": r.tool_name,
            "content": str(r.result) if r.result else str(r.error),
        })
        agent._call_hooks = AsyncMock()
        agent._drain_injections = AsyncMock(return_value=[])
        agent._save_checkpoint = AsyncMock()
        agent._save_denial_checkpoint = AsyncMock()

        node = ToolNode(agent, enable_approval=False, enable_hooks=False)
        history = _TrackingHistory()
        tcs = [
            ToolCall(tool_name="t1", call_id="c1", arguments={}),
            ToolCall(tool_name="t2", call_id="c2", arguments={}),
            ToolCall(tool_name="t3", call_id="c3", arguments={}),
        ]
        ctx = _make_ctx(
            history=history,
            metadata={
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: True,
            },
        )

        t = await node._execute_batch(
            tcs,
            [ApprovalDecision.ALLOWED, ApprovalDecision.DENIED, ApprovalDecision.ALLOWED],
            ctx,
        )
        # t1: executed, t2: denied (stops cascade), t3: preempted
        assert t.target == ReActNode.END
        assert t.reason == ReActReason.TURN_CANCELLED
        assert len(history.appended) == 3  # all three write results


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Memory correctness
# ═══════════════════════════════════════════════════════════════════════════════

class TestMemoryCorrectness:
    """Verify no duplicate messages, correct message ordering."""

    @pytest.mark.asyncio
    async def test_no_duplicate_assistant_messages(self):
        """Each assistant message appears exactly once in history."""
        interceptor = _make_approval_interceptor(
            dangerous_tools=["write_file"], allowed_dirs={"/safe"},
        )
        chain = InterceptorChain(interceptors=[interceptor])
        strategy = SuspendResumeStrategy(
            InMemoryApprovalStateStore(), InMemoryTurnResumeStateStore(),
        )
        history = _TrackingHistory()

        # First pass: LLM response → write assistant with tool_calls
        tc = ToolCall(tool_name="write_file", call_id="c1", arguments={"path": "/etc/hosts"})
        ctx1 = _make_ctx(
            history=history, interceptor_chain=chain, suspend_strategy=strategy,
            metadata={
                ReActMetaKey.LLM_RESPONSE: LLMResponse(
                    content="", tool_calls=[tc], finish_reason="tool_calls",
                ),
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
            },
        )
        # Simulate LLMNode writing assistant message
        await ctx1.history.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": {"path": "/etc/hosts"}}}],
        })

        agent = MagicMock()
        agent._execute_tool = AsyncMock(return_value=ToolResult(tool_name="write_file", result="ok"))
        agent._build_tool_message = MagicMock(return_value={
            "role": "tool", "tool_call_id": "c1", "name": "write_file", "content": "ok",
        })
        agent._call_hooks = AsyncMock()
        agent._drain_injections = AsyncMock(return_value=[])
        agent._save_checkpoint = AsyncMock()

        node = ToolNode(agent, enable_approval=True, enable_hooks=False)
        with pytest.raises(GraphInterrupt):
            await node.execute(ctx1)

        # Resume
        saved = await InMemoryApprovalStateStore().load("s1")
        # Need to use the strategy's store
        approval_store = strategy._approval_store
        saved = await approval_store.load("s1")
        saved.apply("c1", ApprovalDecision.ALLOWED)
        resume_state = await InMemoryTurnResumeStateStore().load("s1")
        resume_state = await strategy._resume_store.load("s1")

        ctx2 = _make_ctx(
            history=history, interceptor_chain=chain, suspend_strategy=strategy,
            metadata={
                ReActMetaKey.RESUME_STATE: resume_state,
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: list(history.appended),
            },
        )
        start = StartNode()
        await start.execute(ctx2)

        _current_resume.set([ApprovalDecision.ALLOWED])
        try:
            node2 = ToolNode(agent, enable_approval=True, enable_hooks=False)
            await node2.execute(ctx2)
        finally:
            _current_resume.set(None)

        # Verify: exactly 1 assistant message (from LLMNode), no duplicates
        assistant_count = sum(1 for m in history.appended if m["role"] == "assistant")
        assert assistant_count == 1, f"Expected 1 assistant, got {assistant_count}"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Cross-turn isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossTurnIsolation:
    """_current_resume and approval state must not leak across turns."""

    @pytest.mark.asyncio
    async def test_current_resume_cleared_after_consumption(self):
        """Strategy clears _current_resume after consuming it."""
        _current_resume.set(["ALLOWED"])
        strategy = SuspendResumeStrategy(
            InMemoryApprovalStateStore(), InMemoryTurnResumeStateStore(),
        )
        result = await strategy.solicit_approval(
            [], MagicMock(), all_tool_calls=[],
        )
        assert result == ["ALLOWED"]
        assert _current_resume.get(None) is None

    def test_current_resume_is_none_for_new_turn(self):
        """After a turn completes, _current_resume is None."""
        _current_resume.set(None)
        assert _current_resume.get(None) is None


# ═══════════════════════════════════════════════════════════════════════════════
# 7. TurnResumeState roundtrip via store
# ═══════════════════════════════════════════════════════════════════════════════

class TestTurnResumeStateRoundtrip:
    """TurnResumeState correctly serializes/deserializes through stores."""

    @pytest.mark.asyncio
    async def test_in_memory_roundtrip(self):
        store = InMemoryTurnResumeStateStore()
        original = TurnResumeState(
            iteration=3,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "rm", "arguments": {"path": "/x"}}}],
            tool_decisions=[ApprovalDecision.PENDING],
            all_new_messages=[{"role": "assistant", "content": "Let me delete..."}],
            llm_content="Let me delete...",
            llm_reasoning=None,
        )
        await store.save("s1", original)
        loaded = await store.load("s1")
        assert loaded is not None
        assert loaded.iteration == 3
        assert len(loaded.tool_calls) == 1
        assert loaded.tool_calls[0]["id"] == "c1"
        assert loaded.tool_calls[0]["function"]["name"] == "rm"
        assert loaded.llm_content == "Let me delete..."
        assert loaded.tool_decisions == [ApprovalDecision.PENDING]

    @pytest.mark.asyncio
    async def test_state_store_roundtrip(self, tmp_path):
        """StateStoreTurnResumeStateStore roundtrip via JsonFileCheckpointStore."""
        from framework.control.checkpoint import JsonFileCheckpointStore
        cp_store = JsonFileCheckpointStore(tmp_path)
        store = StateStoreTurnResumeStateStore(cp_store)
        original = TurnResumeState(
            iteration=2,
            tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": "search", "arguments": {"q": "test"}}},
                {"id": "c2", "type": "function", "function": {"name": "read", "arguments": {"path": "/tmp/x"}}},
            ],
            tool_decisions=[ApprovalDecision.PENDING, ApprovalDecision.ALLOWED],
            all_new_messages=[],
            llm_content="Searching...",
        )
        await store.save("s_x", original)
        loaded = await store.load("s_x")
        assert loaded is not None
        assert loaded.iteration == 2
        assert len(loaded.tool_calls) == 2
        assert loaded.tool_calls[0]["function"]["name"] == "search"
        assert loaded.tool_calls[1]["function"]["name"] == "read"
        assert loaded.llm_content == "Searching..."
        assert loaded.tool_decisions == [ApprovalDecision.PENDING, ApprovalDecision.ALLOWED]
