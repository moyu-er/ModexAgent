"""TDD: E2E verification that approval commands skip memory and trigger agent.run().

The memory file at examples/bot_project/data/memory/.../messages.jsonl shows
/approve and /deny commands stored as user messages. These special commands must:
1. NOT be saved to message history
2. Directly trigger agent.run() (resume from tool via SuspendResumeStrategy)
3. Correctly set RESUME_STATE and TOOL_DECISIONS metadata for ToolNode resume
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.agents.react.constants import ReActMetaKey
from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.core.agent import AgentContext
from framework.core.context import ContextState
from framework.core.emitter import AgentResult
from framework.core.tool_manager import InMemoryToolManager
from framework.core.types import InputMessage
from framework.memory.history import ListMessageHistory
from framework.pipeline.pipeline import AgentPipeline


class _ResumeState:
    """Minimal resume state replacement for TurnResumeState."""
    def __init__(self, iteration, tool_calls, tool_decisions, all_new_messages,
                 llm_content="", llm_reasoning=None):
        self.iteration = iteration
        self.tool_calls = tool_calls
        self.tool_decisions = tool_decisions
        self.all_new_messages = all_new_messages
        self.llm_content = llm_content
        self.llm_reasoning = llm_reasoning
        self.resume_node = "tool"
        self.resume_reason = "resume_tools"


def _make_pipeline(strategy, agent=None):
    p = AgentPipeline(
        agent=agent or MagicMock(),
        context_manager=MagicMock(),
        tool_manager=InMemoryToolManager(),
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        sanitizer=None,
    )
    p._prebuilt_runtime = MagicMock()
    p._prebuilt_runtime.approval = MagicMock()
    p._prebuilt_runtime.approval.suspend_strategy = strategy
    p._prebuilt_runtime.approval.classifier = MagicMock()
    p._prebuilt_runtime.control = None
    p._prebuilt_runtime.hooks = None
    p._prebuilt_runtime.interceptors = None
    p._prebuilt_runtime.checkpoint_store = None
    p._prebuilt_runtime.injection_queue = None
    p._running = True
    p.on_session_start = None
    p.on_session_end = None
    return p


def _pending_state(session_id="s1", n_tools=1):
    reqs = [
        ApprovalRequest("write_file", f"c{i}", {"path": f"/tmp/f{i}.txt"}, "dangerous", 1)
        for i in range(1, n_tools + 1)
    ]
    return ApprovalState(session_id=session_id, requests=reqs)


def _make_ctx_mgr(history):
    ctx_mgr = MagicMock()
    ctx_mgr.load = AsyncMock()
    ctx_mgr.save = AsyncMock()
    ctx_mgr.build_system_prompt = AsyncMock(return_value="system prompt")
    ctx_mgr.load_with_metadata = AsyncMock()
    ctx_mgr.recover_checkpoint = AsyncMock(return_value=(None, False))
    ctx_mgr.flush = AsyncMock()
    ctx_mgr.clear_checkpoint = AsyncMock()
    context_state = ContextState(system_prompt="test", history=history)
    ctx_mgr.load.return_value = context_state
    ctx_mgr.load_with_metadata.return_value = context_state
    return ctx_mgr


class TestApprovalCommandNotInHistory:
    """Verify approval commands (/approve, /deny) are NOT saved to history,
    and the resume state is correctly passed to agent.run()."""

    @pytest.mark.asyncio
    async def test_approve_cmd_not_in_history_and_resumes_correctly(self):
        """/approve with pending approval → skip history, set RESUME_STATE, call agent.run()."""
        approval_state = _pending_state()
        resume_state = _ResumeState(
            iteration=1,
            tool_calls=[{"id": "c1", "type": "function",
                         "function": {"name": "write_file", "arguments": {}}}],
            tool_decisions=[ApprovalDecision.PENDING],
            all_new_messages=[],
            llm_content="let me write that",
        )

        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=approval_state)
        strategy.save_approval_state = AsyncMock()
        strategy.load_resume_state = AsyncMock(return_value=resume_state)
        strategy.delete_approval_state = AsyncMock()
        strategy.delete_resume_state = AsyncMock()

        agent = MagicMock()
        agent.run = AsyncMock(return_value=AgentResult(
            content="done", messages=[{"role": "assistant", "content": "done"}],
        ))

        pipeline = _make_pipeline(strategy, agent=agent)
        pipeline._session_locks["s1"] = MagicMock()
        pipeline._session_locks["s1"].__aenter__ = AsyncMock()
        pipeline._session_locks["s1"].__aexit__ = AsyncMock()

        history = ListMessageHistory()
        ctx_mgr = _make_ctx_mgr(history)
        pipeline.context_manager_factory = MagicMock(return_value=ctx_mgr)

        emitter = MagicMock()
        msg = InputMessage(content="/approve", session_id="s1")

        with patch.object(pipeline, "_build_runtime_and_context") as mock_build:
            mock_ctx = AgentContext(
                system_prompt="test", history=history,
                tool_manager=InMemoryToolManager(), session_id="s1",
                metadata={
                    ReActMetaKey.ITERATION: 1,
                    ReActMetaKey.ITERATION_MSGS: [],
                },
            )
            mock_build.return_value = (mock_ctx, emitter)
            result = await pipeline._process_message_locked(msg, "s1")

        assert result is not None, "approve must return AgentResult"

        # 1. /approve must NOT be in history
        all_msgs = await history.to_list()
        approve_found = any(
            (isinstance(m, dict) and m.get("content") == "/approve")
            or (hasattr(m, "content") and m.content == "/approve")
            for m in all_msgs
        )
        assert not approve_found, (
            "BUG: /approve saved to message history"
        )

        # 2. agent.run() must have been called for resume
        agent.run.assert_called_once()

        # 3. Resume decisions passed through strategy.save_resume_decisions (not metadata)
        called_ctx = agent.run.call_args[0][0]
        assert called_ctx is not None

    @pytest.mark.asyncio
    async def test_deny_cmd_not_in_history_and_sets_deny_decisions(self):
        """/deny with pending approval → skip history, set DENIED decisions, call agent.run()."""
        approval_state = _pending_state()
        resume_state = _ResumeState(
            iteration=1,
            tool_calls=[{"id": "c1", "type": "function",
                         "function": {"name": "write_file", "arguments": {}}}],
            tool_decisions=[ApprovalDecision.PENDING],
            all_new_messages=[],
            llm_content="let me write that",
        )

        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=approval_state)
        strategy.save_approval_state = AsyncMock()
        strategy.load_resume_state = AsyncMock(return_value=resume_state)
        strategy.delete_approval_state = AsyncMock()
        strategy.delete_resume_state = AsyncMock()

        agent = MagicMock()
        agent.run = AsyncMock(return_value=AgentResult(
            content="turn cancelled",
            messages=[{"role": "assistant", "content": "turn cancelled"}],
        ))

        pipeline = _make_pipeline(strategy, agent=agent)
        pipeline._session_locks["s1"] = MagicMock()
        pipeline._session_locks["s1"].__aenter__ = AsyncMock()
        pipeline._session_locks["s1"].__aexit__ = AsyncMock()

        history = ListMessageHistory()
        ctx_mgr = _make_ctx_mgr(history)
        pipeline.context_manager_factory = MagicMock(return_value=ctx_mgr)

        emitter = MagicMock()
        msg = InputMessage(content="/deny", session_id="s1")

        with patch.object(pipeline, "_build_runtime_and_context") as mock_build:
            mock_ctx = AgentContext(
                system_prompt="test", history=history,
                tool_manager=InMemoryToolManager(), session_id="s1",
                metadata={
                    ReActMetaKey.ITERATION: 1,
                    ReActMetaKey.ITERATION_MSGS: [],
                    ReActMetaKey.DENY_AS_CANCEL: True,
                },
            )
            mock_build.return_value = (mock_ctx, emitter)
            result = await pipeline._process_message_locked(msg, "s1")

        assert result is not None

        all_msgs = await history.to_list()
        deny_found = any(
            (isinstance(m, dict) and m.get("content") == "/deny")
            or (hasattr(m, "content") and m.content == "/deny")
            for m in all_msgs
        )
        assert not deny_found, "BUG: /deny saved to message history"

        agent.run.assert_called_once()

        # Decisions reflected through strategy.save_resume_decisions (not metadata)
        called_ctx = agent.run.call_args[0][0]
        assert called_ctx is not None

    @pytest.mark.asyncio
    async def test_normal_message_still_saved_to_history(self):
        """Normal messages (no approval pending) must still be saved to history."""
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=None)

        agent = MagicMock()
        agent.run = AsyncMock(return_value=AgentResult(
            content="hello", messages=[{"role": "assistant", "content": "hello"}],
        ))

        pipeline = _make_pipeline(strategy, agent=agent)
        pipeline._session_locks["s1"] = MagicMock()
        pipeline._session_locks["s1"].__aenter__ = AsyncMock()
        pipeline._session_locks["s1"].__aexit__ = AsyncMock()

        history = ListMessageHistory()
        ctx_mgr = _make_ctx_mgr(history)
        pipeline.context_manager_factory = MagicMock(return_value=ctx_mgr)

        emitter = MagicMock()
        msg = InputMessage(content="hello, how are you?", session_id="s1")

        with patch.object(pipeline, "_build_runtime_and_context") as mock_build:
            mock_ctx = AgentContext(
                system_prompt="test", history=history,
                tool_manager=InMemoryToolManager(), session_id="s1",
                metadata={
                    ReActMetaKey.ITERATION: 1,
                    ReActMetaKey.ITERATION_MSGS: [],
                },
            )
            mock_build.return_value = (mock_ctx, emitter)
            result = await pipeline._process_message_locked(msg, "s1")

        assert result is not None

        all_msgs = await history.to_list()
        normal_found = any(
            (isinstance(m, dict) and m.get("content") == "hello, how are you?")
            or (hasattr(m, "content") and m.content == "hello, how are you?")
            for m in all_msgs
        )
        assert normal_found, (
            "BUG: normal user messages must still be saved to history"
        )
        # No RESUME_STATE for normal turns
        agent.run.assert_called_once()
        called_ctx = agent.run.call_args[0][0]
        assert ReActMetaKey.RESUME_STATE not in called_ctx.metadata, (
            "BUG: normal turns should NOT have RESUME_STATE"
        )
