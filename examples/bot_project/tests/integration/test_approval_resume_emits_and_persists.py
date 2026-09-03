"""Reproduction harness: does the resumed approval turn EMIT + PERSIST?

Why this exists
---------------
The framework capstone (``tests/integration/test_approval_flow_integration.py``)
PROVES the resumed turn EXECUTES the tool when driven via
``pipeline._process_message`` — but it uses a ``_RecordingOutputAdapter`` (no
transcript) and NEVER asserts the resumed turn emits ``tool_call_start`` /
``tool_call_end`` / final text to the emitter.

Production webui reads conversation history from the TRANSCRIPT store (written
by ``WebBotEmitter``), not from the in-memory context manager the capstone
checks. So if the resumed turn executes the tool but does not EMIT/persist to
the transcript, the user sees exactly the reported bug:

  * "审批通过后执行…在webui前端都没有正确渲染tool执行过程" — no live tool render
  * "会话管理中也没有" / "刷新能查到, 但并不能" — transcript empty on refresh
  * "deny all 后没反应" — deny-seal runs backend-side but the resumed turn's
    ``turn_end`` never reaches the frontend (nothing emitted), so the approval
    cards never reconcile/clear.

This module wires a REAL ``WebBotEmitter`` (real transcript store) through
``emitter_factory`` — the exact path production uses — suspends, resumes, and
asserts the transcript actually contains the resumed turn. If the assertion
fails, the root cause is "the resumed turn does not emit/persist"; if it
passes, the bug is further out (transit / workspace resolver) and the next
test in the chain (联调) takes over.
"""
from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

import pytest
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.emitter import WebBotEmitter
from bot.webui.transcript_store import JSONLTranscriptStore

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.agents.react.state import ReActSnapshotPolicy
from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.ui import IMUserInterface
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.events import EmitterConfig
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import Tool
from modex_agent.core.types import InputMessage, LLMResponse, OutputMessage, ToolCall
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.factories.approval import build_approval_runtime
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.runtime.enums import SnapshotReason, TurnPhase
from modex_agent.runtime.models import StateQueryScope
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_agent.tools.manager import InMemoryToolManager


def _make_react_pipeline(
    *,
    agent,
    context_manager=None,
    tool_manager=None,
    input_adapter=None,
    output_adapter=None,
    emitter_factory=None,
    turn_store=None,
    runtime_services=None,
    command_processor=None,
    user_interface=None,
    sanitizer=None,
    max_iterations=10,
    safety=None,
):
    from modex_agent.core.context import InMemoryContextManager
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.pipeline.approval_renderer import ApprovalRenderer
    from modex_agent.pipeline.approval_resumer import ApprovalResumer
    from modex_agent.pipeline.pipeline import AgentPipeline
    from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
    from modex_agent.pipeline.turn_runner import ReActTurnRunner
    from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
    if sanitizer is None:
        from modex_agent.utils.sanitizer import ContentSanitizer
        sanitizer = ContentSanitizer.sanitize
    ctx_mgr = context_manager or InMemoryContextManager()
    resolved_safety = safety or RuntimeSafetyPolicy()
    registry = TurnSessionRegistry()
    builder = TurnContextBuilder(
        agent=agent,
        tool_manager=tool_manager,
        sanitizer=sanitizer,
        command_processor=command_processor,
        skill_resolver=None,
        context_builder=None,
        agent_descriptor=None,
        max_iterations=max_iterations,
        safety=resolved_safety,
        runtime_services=runtime_services,
        runtime_context_manager=None,
        governance=None,
        hook_runner=None,
        interceptor_chain=None,
        control_channel=None,
        emitter_factory=emitter_factory,
        output_adapter=output_adapter,
        turn_store=turn_store,
        registry=registry,
    )
    approval_resumer = ApprovalResumer(agent=agent, turn_store=turn_store, user_interface=user_interface)
    approval = ApprovalRenderer(agent=agent, user_interface=user_interface)
    turn_runner = ReActTurnRunner(
        agent=agent,
        context_manager=ctx_mgr,
        context_manager_factory=None,
        on_session_start=None,
        on_session_end=None,
        safety=resolved_safety,
        turn_store=turn_store,
        registry=registry,
        builder=builder,
        resumer=approval_resumer,
        approval=approval,
        workspace_manager=None,
        pool_name=None,
        pool_data_resolver=None,
        agent_descriptor=None,
    )
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=turn_runner,
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        registry=registry,
        safety=resolved_safety,
        command_processor=command_processor,
    )
    turn_runner.bind_to_pipeline(pipeline)
    return pipeline


pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# Fakes — scripted LLM + recording tool + recording output adapter
# --------------------------------------------------------------------------


class _Provider(CallbackStreamProvider):
    """Scripted LLM: one response per call, replays the last past the end."""

    def __init__(self, script: list[LLMResponse]) -> None:
        super().__init__()
        self._script = list(script)
        self.calls = 0

    def get_default_model(self) -> str:
        return "scripted-test-model"

    async def chat_stream(
        self,
        messages,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs,
    ):
        resp = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return resp


class _WriteTool(Tool):
    """Recording ``write`` tool — gated by the approval config."""

    def __init__(self, recorded: list[tuple[str, str]]) -> None:
        super().__init__(
            name="write",
            description="write a file (recording stub)",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        )
        self._recorded = recorded

    async def execute(self, **kwargs) -> str:
        self._recorded.append((kwargs.get("path", ""), kwargs.get("content", "")))
        return f"wrote {kwargs.get('path', '')}"


class _RecordingOutputAdapter:
    """Records approval-request OutputMessages (IMUserInterface path)."""

    def __init__(self) -> None:
        self.sent: list[OutputMessage] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.sent.append(message)

    async def send_delta(self, delta: str, session_id: str) -> None: ...
    async def flush_deltas(self, session_id: str) -> None: ...

    @property
    def supports_streaming(self) -> bool:
        return False


class _NullInputAdapter:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def receive(self):
        if False:
            yield None


# --------------------------------------------------------------------------
# Harness — real pipeline + real WebBotEmitter (transcript store)
# --------------------------------------------------------------------------


def _build_pipeline_with_webui_emitter(
    *,
    tmp_path: Path,
    sessions_dir: Path,
    turn_store: Any = None,
) -> tuple[
    AgentPipeline,
    _Provider,
    Any,
    list[tuple[str, str]],
    WorkspaceScopedTranscriptStore,
]:
    """Build a real AgentPipeline whose per-turn emitter is a real WebBotEmitter
    backed by a real transcript store — mirroring production's emitter_factory.

    The emitter's ``sessions_dir_provider`` returns a fixed dir so writes do not
    depend on the ``bind_workspace_root`` ContextVar (which is lost in the
    broker consumer task in production).
    """
    recorded: list[tuple[str, str]] = []
    provider = _Provider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
                        arguments={"path": "/etc/secrets", "content": "x"},
                        call_id="c1",
                    ),
                ],
            ),
            LLMResponse(content="done after resume"),
        ]
    )
    agent = ReActAgent(provider)

    tool_manager = InMemoryToolManager()
    tool_manager.register(_WriteTool(recorded))

    approval_runtime = build_approval_runtime(
        ApprovalConfig(enabled=True, tools={"write": ToolApprovalEntry(allowed_paths=["./*"])}),
        project_root=tmp_path,
    )
    turn_store = turn_store if turn_store is not None else InMemoryTurnStateStore()
    runtime_services = AgentRuntimeServices(approval=approval_runtime, turn_store=turn_store)

    recording_output = _RecordingOutputAdapter()

    # Real webui output path: WS input adapter owns the delta queues; the WS
    # output adapter enqueues envelopes there (dropped when no queue is
    # registered — fine, we assert on the transcript store, not live deltas).
    ws_input = WebSocketInputAdapter()
    ws_output = WebSocketOutputAdapter(ws_input)
    transcript_store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

    def emitter_factory(session_id: str, pool: str) -> WebBotEmitter:
        assert pool == ""
        return WebBotEmitter(
            output_adapter=ws_output,
            session_id=session_id,
            config=EmitterConfig(),
            pool=pool,
            transcript_store=transcript_store,
            sessions_dir_provider=lambda: sessions_dir,
        )

    pipeline = _make_react_pipeline(
        agent=agent,
        context_manager=InMemoryContextManager(),
        tool_manager=tool_manager,
        input_adapter=_NullInputAdapter(),
        output_adapter=recording_output,
        sanitizer=None,
        turn_store=turn_store,
        runtime_services=runtime_services,
        emitter_factory=partial(emitter_factory, pool=""),
        user_interface=IMUserInterface(output_adapter=recording_output),
    )
    return pipeline, provider, turn_store, recorded, transcript_store


# ==========================================================================
# Test — resumed turn must emit + persist to the transcript store
# ==========================================================================


@pytest.mark.asyncio
async def test_resumed_approval_turn_persists_to_transcript(tmp_path: Path) -> None:
    """Suspend -> webui ALLOW resume -> the resumed turn's tool_call + final
    text MUST be persisted to the transcript store (what GET /messages reads).

    Failure here = root cause of "刷新查不到 / 没渲染 / 会话管理没有": the resumed
    turn runs but does not emit/persist.
    """
    sessions_dir = tmp_path / ".modex" / "sessions"
    pipeline, provider, turn_store, recorded, transcript_store = (
        _build_pipeline_with_webui_emitter(tmp_path=tmp_path, sessions_dir=sessions_dir)
    )
    session = SessionInfo.from_str("s1.main")

    # --- Step 1: dangerous write -> suspend ---
    assert await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    ) is None
    assert recorded == [], "tool must not execute before approval"

    # --- Step 2: webui ALLOW -> resume ---
    resume_result = await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
        )
    )
    assert resume_result is not None, "resumed turn must complete"
    assert recorded == [("/etc/secrets", "x")], f"tool should execute after allow, got {recorded}"

    # --- Step 3: the resumed turn MUST have persisted to the transcript ---
    # GET /messages reads via load_sessions_by_prefix; the file lives under
    # <sessions_dir>/<pool>/<session_id>.jsonl.
    transcript_file = sessions_dir / "main" / "s1.main.jsonl"
    assert transcript_file.exists(), (
        f"resumed turn transcript missing: {transcript_file} (children={list(sessions_dir.rglob('*')) if sessions_dir.exists() else 'no sessions dir'})"
    )
    events = await JSONLTranscriptStore(sessions_dir / "main").load("s1.main")
    event_types = [type(e).__name__ for e in events]
    # The tool that executed on resume must appear in the transcript.
    assert "ToolCallEvent" in event_types, (
        f"resumed turn did not persist ToolCallEvent; got {event_types}"
    )
    assert "ToolResultEvent" in event_types, (
        f"resumed turn did not persist ToolResultEvent; got {event_types}"
    )


@pytest.mark.asyncio
async def test_resumed_approval_turn_persists_final_text(tmp_path: Path) -> None:
    """The resumed turn's final assistant text ("done after resume") must also
    reach the transcript — otherwise the turn looks empty on refresh even though
    the tool ran."""
    sessions_dir = tmp_path / ".modex" / "sessions"
    pipeline, provider, turn_store, recorded, transcript_store = (
        _build_pipeline_with_webui_emitter(tmp_path=tmp_path, sessions_dir=sessions_dir)
    )
    session = SessionInfo.from_str("s1.main")

    assert await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    ) is None
    await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
        )
    )

    events = await JSONLTranscriptStore(sessions_dir / "main").load("s1.main")
    blobs = [str(e.to_dict()) for e in events]
    assert any("done after resume" in b for b in blobs), (
        f"resumed turn final text missing from transcript; events={blobs}"
    )


# ==========================================================================
# DECISIVE variant — REAL serialization round-trip (JsonFileTurnStateStore)
#
# InMemoryTurnStateStore keeps the LIVE ReActTurnState object, so a save/load
# "round-trip" never serializes — phase / current_node / the approval
# transaction all stay intact by reference. Production persists the snapshot to
# JSON and reconstructs the state via the codec on resume. If any field that
# positions the graph (phase, current_node) or carries the approval decision
# does not survive the round-trip, the resumed turn re-enters at the wrong node
# and the tool never re-emits / never persists — exactly "tool 执行前中断,
# 恢复失败". This variant closes that blind spot.
# ==========================================================================


@pytest.mark.asyncio
async def test_resumed_turn_persists_after_jsonfile_snapshot_roundtrip(
    tmp_path: Path,
) -> None:
    """Same as the in-memory variant but with the REAL JsonFileTurnStateStore.

    The snapshot is serialized to disk on suspend and deserialized on resume
    (the production path). If the resumed turn fails to re-emit/persist here
    while the in-memory variant passes, the root cause is a snapshot
    serialization round-trip defect.
    """
    from modex_agent.agents.react.state import ReActRuntimeStateCodec
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry
    from modex_agent.runtime.enums import AgentKind
    from modex_agent.runtime.store import JsonFileTurnStateStore

    codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
    turn_store = JsonFileTurnStateStore(tmp_path / "turns", codec_registry)

    sessions_dir = tmp_path / ".modex" / "sessions"
    pipeline, provider, _, recorded, transcript_store = (
        _build_pipeline_with_webui_emitter(
            tmp_path=tmp_path, sessions_dir=sessions_dir, turn_store=turn_store
        )
    )
    session = SessionInfo.from_str("s1.main")

    # Step 1: dangerous write -> suspend (snapshot serialized to disk).
    assert await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    ) is None
    assert recorded == [], "tool must not execute before approval"

    # Step 2: webui ALLOW -> resume (snapshot deserialized + restored).
    resume_result = await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
        )
    )
    assert resume_result is not None, "resumed turn must complete after JsonFile round-trip"
    assert recorded == [("/etc/secrets", "x")], (
        f"tool should execute after allow (round-trip); got {recorded}"
    )

    # Step 3: transcript MUST contain the resumed turn despite the round-trip.
    transcript_file = sessions_dir / "main" / "s1.main.jsonl"
    assert transcript_file.exists(), (
        f"resumed turn transcript missing after round-trip; "
        f"children={list(sessions_dir.rglob('*')) if sessions_dir.exists() else 'no sessions dir'}"
    )
    events = await JSONLTranscriptStore(sessions_dir / "main").load("s1.main")
    event_types = [type(e).__name__ for e in events]
    assert "ToolCallEvent" in event_types, (
        f"resumed turn did not persist ToolCallEvent after round-trip; got {event_types}"
    )
    assert "ToolResultEvent" in event_types, (
        f"resumed turn did not persist ToolResultEvent after round-trip; got {event_types}"
    )


# ==========================================================================
# DECISIVE materialization test — the bug users see ("刷新后没有 tool 渲染")
#
# Persistence alone is not enough: GET /messages MATERIALIZES the transcript
# (groups by turn_id, pairs tool_call->tool_result by call_id). On a resumed
# approval turn the tool node emits only TOOL_CALL_END (the call was already
# decided in the suspended snapshot), so:
#   * the tool_result is persisted with turn_id="" (emitter never called
#     _ensure_turn_started for the resumed turn) -> DROPPED by the materializer
#     (`and evt.turn_id` filter);
#   * the tool_call sits in the SUSPENDED turn's turn_id -> orphaned, no
#     matching result in its group -> not rendered.
# Net effect: refresh shows zero tool blocks even though the transcript file
# has the events. This test materializes the resumed transcript and asserts a
# complete tool block (tool + args + result) survives.
# ==========================================================================


@pytest.mark.asyncio
async def test_resumed_turn_materializes_into_complete_tool_block(tmp_path: Path) -> None:
    """After suspend + resume, GET /messages materialization must yield a turn
    whose blocks contain the tool with BOTH args and result."""
    sessions_dir = tmp_path / ".modex" / "sessions"
    pipeline, provider, _, recorded, transcript_store = (
        _build_pipeline_with_webui_emitter(tmp_path=tmp_path, sessions_dir=sessions_dir)
    )
    session = SessionInfo.from_str("s1.main")

    assert await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    ) is None
    await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
        )
    )
    assert recorded == [("/etc/secrets", "x")]

    # Materialize exactly like GET /messages does.
    turns = await JSONLTranscriptStore(
        sessions_dir / "main"
    ).load_materialized_by_prefix("s1")
    tool_blocks = [
        b
        for t in turns
        for b in t.blocks
        if b.get("kind") == "tool"
    ]
    assert tool_blocks, (
        f"resumed turn did not materialize any tool block; turns={[t.blocks for t in turns]}"
    )
    write_block = next((b for b in tool_blocks if b.get("tool") == "write"), None)
    assert write_block is not None, f"write tool block missing; tool_blocks={tool_blocks}"
    # args must survive (the call) AND result must survive (the execution).
    assert write_block.get("args"), f"tool block lost its args: {write_block}"
    assert write_block.get("result"), f"tool block lost its result: {write_block}"


# ==========================================================================
# DENIAL feedback — the tool message fed back to the agent on DENY must be
# explicit. The old code wrote an opaque ``"Error: Error: denied"`` (captured
# in the user's log, issue/temp.txt:81), so the agent could not tell its tool
# call had been REJECTED by the user. It then re-issued the same dangerous
# tool in the next ReAct iteration -> fresh GraphInterrupt -> a NEW approval
# card -> the user's "deny all 后卡住, 前端又出现一个 toolCall 要审批".
#
# The batch-seal itself is correct (deny preempts all siblings); the bug is
# purely the feedback message. This test reads back the agent's history (the
# messages.jsonl equivalent) and asserts the denial is human/LLM-readable.
# ==========================================================================


@pytest.mark.asyncio
async def test_denied_tool_result_message_is_explicit_rejection(tmp_path: Path) -> None:
    """On DENY, the tool-result message in the agent's history MUST explicitly
    state the user rejected the call and instruct the agent not to retry."""
    sessions_dir = tmp_path / ".modex" / "sessions"
    pipeline, *_rest = _build_pipeline_with_webui_emitter(
        tmp_path=tmp_path, sessions_dir=sessions_dir
    )
    session = SessionInfo.from_str("s1.main")

    # 1. dangerous write -> suspend
    assert await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    ) is None

    # 2. user DENIES (webui "deny" / "deny all")
    await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.DENY),
        )
    )

    # 3. read back the agent's history (what messages.jsonl persists)
    ctx_mgr = pipeline._turn_runner.context_manager
    assert ctx_mgr is not None
    state = ctx_mgr.get_session_state(session.session_id)
    assert state is not None
    messages = await state.history.to_list()
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs, f"no tool message recorded after deny; messages={messages}"
    denied_content = str(tool_msgs[-1].get("content"))

    # MUST clearly convey the user rejected it and that ONLY THIS INVOCATION
    # was disallowed (problem 2 core: agent must not think the tool is banned).
    assert "denied by user" in denied_content.lower(), (
        f"denial message does not say the user denied it: {denied_content!r}"
    )
    assert "this invocation is not allowed" in denied_content.lower(), (
        f"denial message does not say this invocation was disallowed: {denied_content!r}"
    )
    assert "the tool itself is not banned" in denied_content.lower(), (
        f"denial message does not clarify the tool itself is still available: {denied_content!r}"
    )
    # MUST instruct the agent not to retry (problem 1: stops the retry loop).
    assert "must not be retried" in denied_content.lower(), (
        f"denial message does not tell the agent to stop retrying: {denied_content!r}"
    )
    # MUST NOT carry the broken double 'Error: Error:' prefix.
    assert "error: error:" not in denied_content.lower(), (
        f"denial message still has the double Error prefix: {denied_content!r}"
    )


# ==========================================================================
# Deny-All batch seal test — proves the user's suspicion is NOT "backend
# didn't deny everything". WebUI 'Deny All' sends a DENY for ONE card only;
# the backend preempts all siblings. This test suspends with TWO dangerous
# tools, sends DENY only for c1, and asserts BOTH are rejected and the turn
# COMPLETES (no lingering pending snapshot).
# ==========================================================================


@pytest.mark.asyncio
async def test_deny_all_on_batch_seals_all_pending_requests(tmp_path: Path) -> None:
    """A single DENY decision on a multi-tool batch must preempt every sibling
    request so the resumed turn completes without re-suspending."""
    sessions_dir = tmp_path / ".modex" / "sessions"
    recorded: list[tuple[str, str]] = []
    provider = _Provider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
                        arguments={"path": "/etc/a", "content": "a"},
                        call_id="c1",
                    ),
                    ToolCall(
                        tool_name="write",
                        arguments={"path": "/etc/b", "content": "b"},
                        call_id="c2",
                    ),
                ],
            ),
            LLMResponse(content="Both writes were denied by the user."),
        ]
    )
    agent = ReActAgent(provider)

    tool_manager = InMemoryToolManager()
    tool_manager.register(_WriteTool(recorded))

    approval_runtime = build_approval_runtime(
        ApprovalConfig(enabled=True, tools={"write": ToolApprovalEntry(allowed_paths=["./*"])}),
        project_root=tmp_path,
    )
    turn_store = InMemoryTurnStateStore()
    runtime_services = AgentRuntimeServices(approval=approval_runtime, turn_store=turn_store)

    recording_output = _RecordingOutputAdapter()
    ws_input = WebSocketInputAdapter()
    ws_output = WebSocketOutputAdapter(ws_input)
    transcript_store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

    def emitter_factory(session_id: str, pool: str) -> WebBotEmitter:
        assert pool == ""
        return WebBotEmitter(
            output_adapter=ws_output,
            session_id=session_id,
            config=EmitterConfig(),
            pool=pool,
            transcript_store=transcript_store,
            sessions_dir_provider=lambda: sessions_dir,
        )

    pipeline = _make_react_pipeline(
        agent=agent,
        context_manager=InMemoryContextManager(),
        tool_manager=tool_manager,
        input_adapter=_NullInputAdapter(),
        output_adapter=recording_output,
        sanitizer=None,
        turn_store=turn_store,
        runtime_services=runtime_services,
        emitter_factory=partial(emitter_factory, pool=""),
        user_interface=IMUserInterface(output_adapter=recording_output),
    )

    session = SessionInfo.from_str("s1.main")

    # 1. Two dangerous writes -> one suspend with two pending requests.
    assert await pipeline._process_message(
        InputMessage(content="write both files", session=session)
    ) is None
    assert recorded == [], "no tool executes before approval"

    pending = await turn_store.list_active_turns(
        StateQueryScope(
            session_id="s1.main",
            phase=TurnPhase.SUSPENDED,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        )
    )
    assert len(pending) == 1, f"expected one pending snapshot, got {len(pending)}"
    approval = ReActSnapshotPolicy.approval_from_snapshot(pending[0])
    assert approval is not None
    assert len(approval.requests) == 2, f"expected 2 requests, got {len(approval.requests)}"

    # 2. User clicks "Deny All" on the first card -> sends DENY for c1 ONLY.
    result = await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.DENY),
        )
    )
    assert result is not None, "deny-all must complete the resumed turn, not re-suspend"

    # 3. Neither tool should have executed; no pending snapshots left.
    assert recorded == [], "no tool should execute after deny-all"
    pending_after = await turn_store.list_active_turns(
        StateQueryScope(
            session_id="s1.main",
            phase=TurnPhase.SUSPENDED,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        )
    )
    assert pending_after == [], f"pending snapshot leaked after deny-all: {pending_after}"

    # 4. The agent's history contains two tool results, both explicit rejections.
    ctx_mgr = pipeline._turn_runner.context_manager
    assert ctx_mgr is not None
    state = ctx_mgr.get_session_state(session.session_id)
    assert state is not None
    messages = await state.history.to_list()
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 2, f"expected 2 tool messages after deny-all, got {tool_msgs}"
    for tm in tool_msgs:
        content = str(tm.get("content"))
        assert "this invocation is not allowed" in content.lower() or "was not allowed" in content.lower(), (
            f"tool result is not explicit: {content!r}"
        )
        assert "must not be retried" in content.lower() or "do not retry" in content.lower(), (
            f"tool result does not stop retry: {content!r}"
        )
