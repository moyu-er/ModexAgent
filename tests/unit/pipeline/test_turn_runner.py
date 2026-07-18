"""ReActTurnRunner — locked-message processing + turn execution (extracted from pipeline).

Unit tests for the three responsibilities moved out of ``AgentPipeline``:
``execute_turn`` (GraphInterrupt handling + finally cleanup), the
``_handle_snapshot_approval`` driver, and ``process_locked`` delegation.

These construct the runner directly (no AgentPipeline) so the tests target
the runner's own contract, not the pipeline's wiring. Fixture patterns are
adapted from ``test_pipeline_interrupt.py`` and ``test_pipeline_cleanup.py``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.approval.types import ApprovalAction
from modex_agent.core.agent import AgentContext
from modex_agent.core.context import ContextState, InMemoryContextManager
from modex_agent.core.emitter import AgentResult
from modex_agent.core.graph.interrupt import GraphInterrupt
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import InputMessage
from modex_agent.pipeline.approval_renderer import ApprovalRenderer
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.models import TurnSnapshot
from modex_agent.runtime.store import InMemoryTurnStateStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingUI:
    """Captures render_message / render_approval_prompt calls so tests can assert approval prompts."""

    def __init__(self) -> None:
        self.rendered_prompt: str | None = None

    async def render_message(self, session_id: str, content: str) -> None:
        self.rendered_prompt = content

    async def render_approval_prompt(self, session_id: str, view) -> None:
        # Record the same way render_message does so existing assertions
        # (``assert ui.rendered_prompt is not None``) stay valid.
        self.rendered_prompt = view.tool_name


class _InterruptingAgent:
    """agent.run raises GraphInterrupt to exercise the approval-suspend path."""

    name = "interrupting-agent"

    async def run(self, context: AgentContext, emitter: Any) -> AgentResult:
        # GraphInterrupt.value is a list of ApprovalRequestState-like objects;
        # execute_turn reads req.tool_name / tool_call_id / tier / arguments.
        req = MagicMock()
        req.tool_name = "dangerous_tool"
        req.tool_call_id = "call_1"
        req.tier = "high"
        req.arguments = MagicMock(values={"x": 1})
        raise GraphInterrupt(value=[req])


class _OkAgent:
    """agent.run returns a plain AgentResult."""

    name = "ok-agent"

    async def run(self, context: AgentContext, emitter: Any) -> AgentResult:
        return AgentResult(content="ok", stop_reason="stop")


class _FlushingCtxMgr(InMemoryContextManager):
    """InMemoryContextManager that records flush() calls."""

    def __init__(self) -> None:
        super().__init__()
        self.flushed = False

    async def flush(self, session_id: str) -> None:  # type: ignore[override]
        self.flushed = True


def _make_agent_context(session_id: str = "s1") -> AgentContext:
    """Build a minimal AgentContext usable by execute_turn."""
    return AgentContext(
        system_prompt="sys",
        history=ContextState().history,
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_id + ".main"),
    )


def _make_runner(
    *,
    agent: Any = None,
    turn_store: Any = None,
    user_interface: Any = None,
    approval: ApprovalRenderer | None = None,
    resumer: ApprovalResumer | None = None,
    builder: TurnContextBuilder | None = None,
    registry: TurnSessionRegistry | None = None,
    on_session_end: Any = None,
) -> ReActTurnRunner:
    """Construct a ReActTurnRunner with sane defaults; tests override what they exercise."""
    agent = agent or _OkAgent()
    turn_store = turn_store if turn_store is not None else InMemoryTurnStateStore()
    registry = registry or TurnSessionRegistry()
    ui = user_interface if user_interface is not None else _RecordingUI()
    approval = approval or ApprovalRenderer(agent=agent, user_interface=ui)
    resumer = resumer or ApprovalResumer(agent=agent, turn_store=turn_store, user_interface=ui)
    if builder is None:
        builder = MagicMock(spec=TurnContextBuilder)

    return ReActTurnRunner(
        agent=agent,
        context_manager=_FlushingCtxMgr(),
        context_manager_factory=None,
        on_session_start=None,
        on_session_end=on_session_end,
        safety=RuntimeSafetyPolicy(),
        turn_store=turn_store,
        registry=registry,
        builder=builder,
        resumer=resumer,
        approval=approval,
        workspace_manager=None,
        pool_name=None,
        pool_data_resolver=None,
        agent_descriptor=None,
    )


# ---------------------------------------------------------------------------
# execute_turn
# ---------------------------------------------------------------------------


async def test_execute_turn_swallows_graph_interrupt_renders_prompt() -> None:
    """agent.run raises GraphInterrupt -> execute_turn renders prompt, returns None."""
    ui = _RecordingUI()
    runner = _make_runner(agent=_InterruptingAgent(), user_interface=ui)
    agent_context = _make_agent_context()
    ctx_mgr = _FlushingCtxMgr()

    result = await runner.execute_turn(
        agent_context,
        MagicMock(),
        "s1",
        ContextState(),
        {},
        ctx_mgr,
    )

    assert result is None
    assert ui.rendered_prompt is not None


async def test_execute_turn_finally_unregisters_and_flushes() -> None:
    """finally block: unregister turn + flush memory (via _safe_flush)."""
    registry = TurnSessionRegistry()
    ctx_mgr = _FlushingCtxMgr()
    runner = _make_runner(registry=registry)

    # Simulate an active task registration so unregister has something to clear.
    import asyncio

    registry.register_task("s1", asyncio.current_task())

    await runner.execute_turn(
        _make_agent_context(),
        MagicMock(),
        "s1",
        ContextState(),
        {},
        ctx_mgr,
    )

    assert registry.is_active("s1") is False
    assert ctx_mgr.flushed is True


async def test_execute_turn_returns_result_on_success() -> None:
    """A normal agent turn returns the AgentResult and persists via ctx_mgr.save."""
    ctx_mgr = _FlushingCtxMgr()
    runner = _make_runner(agent=_OkAgent())

    result = await runner.execute_turn(
        _make_agent_context(),
        MagicMock(),
        "s1",
        ContextState(),
        {},
        ctx_mgr,
    )

    assert result is not None
    assert result.stop_reason == "stop"


async def test_execute_turn_finally_runs_on_session_end() -> None:
    """on_session_end is awaited in the finally block (with timeout guard)."""
    called = False

    async def _on_end(session_id: str) -> None:
        nonlocal called
        called = True

    runner = _make_runner(on_session_end=_on_end)

    await runner.execute_turn(
        _make_agent_context(),
        MagicMock(),
        "s1",
        ContextState(),
        {},
        _FlushingCtxMgr(),
    )

    assert called


# ---------------------------------------------------------------------------
# _handle_snapshot_approval driver
# ---------------------------------------------------------------------------


async def test_handle_snapshot_approval_returns_none_when_resume_not_applied() -> None:
    """When apply_resume returns None the driver returns None without executing."""
    resumer = MagicMock(spec=ApprovalResumer)
    resumer.apply_resume = AsyncMock(return_value=None)
    runner = _make_runner(resumer=resumer)

    snapshot = MagicMock(spec=TurnSnapshot)

    result = await runner._handle_snapshot_approval(
        action=ApprovalAction.ALLOW,
        snapshot=snapshot,
        agent_context=_make_agent_context(),
        emitter=MagicMock(),
        session_id="s1",
        context_state=ContextState(),
        input_metadata={},
        ctx_mgr=_FlushingCtxMgr(),
    )

    assert result is None
    resumer.apply_resume.assert_awaited_once()


async def test_handle_snapshot_approval_drains_on_success() -> None:
    """When resume + execute succeed the driver deletes the snapshot + drains."""
    turn_store = InMemoryTurnStateStore()
    approval = MagicMock(spec=ApprovalRenderer)
    approval.drain = AsyncMock()
    resumer = MagicMock(spec=ApprovalResumer)
    resumer.apply_resume = AsyncMock(return_value=turn_store)
    runner = _make_runner(
        agent=_OkAgent(),
        turn_store=turn_store,
        approval=approval,
        resumer=resumer,
    )

    snapshot = MagicMock(spec=TurnSnapshot)
    snapshot.identity = MagicMock()

    result = await runner._handle_snapshot_approval(
        action=ApprovalAction.ALLOW,
        snapshot=snapshot,
        agent_context=_make_agent_context(),
        emitter=MagicMock(),
        session_id="s1",
        context_state=ContextState(),
        input_metadata={},
        ctx_mgr=_FlushingCtxMgr(),
    )

    assert result is not None
    approval.drain.assert_awaited_once_with("s1")


# ---------------------------------------------------------------------------
# _resolve_pool_data / _is_subagent
# ---------------------------------------------------------------------------


async def test_resolve_pool_data_returns_none_without_workspace_manager() -> None:
    """No workspace_manager -> always None (backward-compatible static path off)."""
    runner = _make_runner()
    assert runner._resolve_pool_data("s1") is None


def test_is_subagent_false_without_descriptor() -> None:
    """No agent_descriptor -> not a subagent."""
    runner = _make_runner()
    assert runner._is_subagent() is False


# ---------------------------------------------------------------------------
# process_locked (high-level delegation smoke)
# ---------------------------------------------------------------------------


async def test_process_locked_binds_workspace_root_from_manager(
    tmp_path, monkeypatch
) -> None:
    """Regression (broker-boundary contextvar loss): the turn executes on the
    pool's broker-consumer task, which does NOT inherit the dispatcher's
    ``bind_workspace_root`` across the broker queue. ``process_locked`` must
    re-bind the active workspace root (resolved from ``workspace_manager``, a
    contextvar-independent per-workspace source) so attachment resolution
    (mechanism A inline images + mechanism B path references) resolves against
    the real workspace root — NOT the process CWD (the bot install dir).

    Without the bind, ``resolve_workspace_root()`` falls back to ``Path.cwd()``
    and every image silently degrades to ``<missing image>`` in non-HOME
    workspaces (where CWD ≠ workspace root).
    """
    from modex_agent.workspace.runtime import resolve_workspace_root

    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    # Process CWD is elsewhere entirely (mirrors prod: bot install dir).
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    seen: dict[str, Any] = {}

    builder = MagicMock(spec=TurnContextBuilder)
    builder.build_turn_request = AsyncMock(
        return_value=MagicMock(
            user_content=None,
            append_user_message=True,
            trigger_agent=True,
            approval_action=None,
            command_result=None,
        )
    )

    async def _capture_preprocess(*args: Any, **kwargs: Any):
        # Mechanism B (_attachment_reference) resolves paths here — capture the
        # bound root at exactly this point in the turn.
        seen["ws_root"] = resolve_workspace_root()
        return ("hi", [], None)

    builder.preprocess = AsyncMock(side_effect=_capture_preprocess)
    builder.assemble = AsyncMock(return_value=ContextState())
    builder.build_runtime_and_context = MagicMock(
        return_value=(_make_agent_context(), MagicMock())
    )

    approval = MagicMock(spec=ApprovalRenderer)
    approval.detect = AsyncMock(return_value=(False, None))

    # Per-workspace fixed resources (contextvar-independent) — the same source
    # _resolve_pool_data already reads. pool_name stays None so pool_data is None.
    ws_resources = MagicMock()
    ws_resources.workspace_root = ws_root
    ws_resources.pool_data = {}
    workspace_manager = MagicMock()
    workspace_manager.resolve_workspace.return_value = ws_resources

    runner = _make_runner(agent=_OkAgent(), builder=builder, approval=approval)
    runner._workspace_manager = workspace_manager

    input_msg = InputMessage(content="hi", session=SessionInfo.from_str("s1.main"))
    await runner.process_locked(
        input_msg,
        "s1",
        None,
        session=SessionInfo.from_str("s1.main"),
    )

    assert seen["ws_root"].resolve() == ws_root.resolve(), (
        "process_locked did not bind the workspace root from workspace_manager; "
        "attachment resolution would fall back to the process CWD"
    )


async def test_process_locked_runs_full_flow_and_returns_result() -> None:
    """process_locked composes builder + approval + execute_turn end-to-end."""
    builder = MagicMock(spec=TurnContextBuilder)
    builder.build_turn_request = AsyncMock(
        return_value=MagicMock(
            user_content=None,
            append_user_message=True,
            trigger_agent=True,
            approval_action=None,
            command_result=None,
        )
    )
    builder.preprocess = AsyncMock(return_value=("hi", [], None))
    builder.assemble = AsyncMock(return_value=ContextState())
    builder.build_runtime_and_context = MagicMock(
        return_value=(_make_agent_context(), MagicMock())
    )

    approval = MagicMock(spec=ApprovalRenderer)
    approval.detect = AsyncMock(return_value=(False, None))

    runner = _make_runner(agent=_OkAgent(), builder=builder, approval=approval)
    input_msg = InputMessage(content="hi", session=SessionInfo.from_str("s1.main"))

    result = await runner.process_locked(
        input_msg,
        "s1",
        None,
        session=SessionInfo.from_str("s1.main"),
    )

    assert result is not None
    assert result.stop_reason == "stop"
