"""Pipeline-level end-to-end approval flow (capstone verification).

Drives the REAL ``AgentPipeline`` (real ReAct graph, real
``build_approval_runtime`` factory, real ``SlashCommandProcessor.default()``,
real ``InMemoryTurnStateStore``, real ``InMemoryContextManager``) through the
approval wiring the feature added:

  - DANGEROUS tool call -> GraphInterrupt (caught inside ``TurnRunner.execute_turn``)
    -> snapshot persisted to turn_store -> structured approval prompt rendered
    (``message_type="approval_request"``).
  - Webui decision (``InputMessage.approval_decision``) -> ``_process_message``
    -> dedup-skip -> ``build_turn_request`` short-circuit -> ``_handle_snapshot_approval``
    -> ``apply_resume(tool_call_id=...)`` -> ``execute_turn`` -> tool executes
    -> snapshot deleted.
  - IM ``/approve`` path via the command processor.
  - Path tiering: in-project path -> NORMAL -> no suspend; out-of-project ->
    DANGEROUS -> suspend. And default-off (``enabled=False``) -> no suspend.

The only fakes are a scripted LLM provider and a recording ``write_file`` tool.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.approval.ui import IMUserInterface
from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.commands.processor import SlashCommandProcessor
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, Tool
from modex_agent.core.types import InputMessage, LLMResponse, OutputMessage, ToolCall
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.factories.approval import build_approval_runtime
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.runtime.enums import SnapshotReason, TurnPhase
from modex_agent.runtime.models import StateQueryScope
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# Fakes — the only test doubles (scripted LLM + recording tool)
# --------------------------------------------------------------------------


class _Provider:
    """Script-driven LLM provider: returns one ``LLMResponse`` per call.

    Past the end of the script it keeps replaying the last response, so a
    resume that needs a final no-tool-call response works regardless of how
    many extra calls the graph makes.
    """

    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.calls = 0

    async def chat(self, messages, **kwargs):
        resp = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return resp


class _WriteFileTool(Tool):
    """Recording ``write_file`` tool — matches the production tool name.

    The approval classifier does a LITERAL ``config.tools.get(tool_name)``
    lookup, so this name, the ``ToolCall.tool_name`` the provider emits, and
    the approval-config key must all be the same string. Execution just needs
    to succeed and record its arguments.
    """

    def __init__(self, recorded: list[tuple[str, str]]) -> None:
        super().__init__(
            name="write_file",
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

    async def execute(self, **kwargs):
        self._recorded.append((kwargs.get("path", ""), kwargs.get("content", "")))
        return f"wrote {kwargs.get('path', '')}"


class _InputAdapter:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def receive(self):
        if False:
            yield None


class _RecordingOutputAdapter:
    """Records every ``OutputMessage`` sent so tests can assert on
    ``message_type == "approval_request"``."""

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


# --------------------------------------------------------------------------
# Construction helper — full AgentPipeline (preferred level)
# --------------------------------------------------------------------------


def _build_pipeline(
    *,
    tmp_path: Path,
    enabled: bool = True,
    allowed_paths: list[str] | None = None,
    command_processor: SlashCommandProcessor | None = None,
) -> tuple[
    AgentPipeline, _Provider, _RecordingOutputAdapter, InMemoryTurnStateStore, list[tuple[str, str]]
]:
    """Build a full AgentPipeline wired for the approval flow.

    Returns ``(pipeline, provider, output_adapter, turn_store, recorded)`` so
    tests can drive ``_process_message`` and inspect the store / output /
    provider / tool-execution log.
    """
    recorded: list[tuple[str, str]] = []
    output_adapter = _RecordingOutputAdapter()
    turn_store = InMemoryTurnStateStore()

    # Script: call 1 emits the dangerous write_file tool call; call 2 returns
    # a plain content response so the resumed turn completes. Tests that need
    # a different script build their own provider and pass a fresh agent via
    # _build_pipeline_with_agent.
    provider = _Provider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write_file",
                        arguments={"path": "/etc/passwd", "content": "x"},
                        call_id="c1",
                    ),
                ],
            ),
            LLMResponse(content="done"),
        ]
    )
    agent = ReActAgent(provider)

    tool_manager = InMemoryToolManager()
    tool_manager.register(_WriteFileTool(recorded))

    approval_cfg = ApprovalConfig(
        enabled=enabled,
        tools={"write_file": ToolApprovalEntry(allowed_paths=allowed_paths or ["./*"])},
    )
    approval_runtime = build_approval_runtime(approval_cfg, project_root=tmp_path)

    runtime_services = AgentRuntimeServices(
        approval=approval_runtime,
        turn_store=turn_store,
    )

    pipeline = AgentPipeline(
        agent=agent,
        context_manager=InMemoryContextManager(),
        tool_manager=tool_manager,
        input_adapter=_InputAdapter(),
        output_adapter=output_adapter,
        sanitizer=None,
        turn_store=turn_store,
        runtime_services=runtime_services,
        command_processor=command_processor,
        # Real IMUserInterface so the suspend path actually emits the
        # structured approval_request OutputMessage through the adapter.
        user_interface=IMUserInterface(output_adapter=output_adapter),
    )
    return pipeline, provider, output_adapter, turn_store, recorded


def _build_pipeline_with_agent(
    *,
    tmp_path: Path,
    agent: ReActAgent,
    output_adapter: _RecordingOutputAdapter,
    turn_store: InMemoryTurnStateStore,
    recorded: list[tuple[str, str]],
    enabled: bool = True,
    allowed_paths: list[str] | None = None,
    command_processor: SlashCommandProcessor | None = None,
) -> AgentPipeline:
    """Build a pipeline around an already-constructed agent + stores.

    Used by path-tiering / default-off tests that feed a different provider
    script.
    """
    tool_manager = InMemoryToolManager()
    tool_manager.register(_WriteFileTool(recorded))

    approval_cfg = ApprovalConfig(
        enabled=enabled,
        tools={"write_file": ToolApprovalEntry(allowed_paths=allowed_paths or ["./*"])},
    )
    approval_runtime = build_approval_runtime(approval_cfg, project_root=tmp_path)

    runtime_services = AgentRuntimeServices(
        approval=approval_runtime,
        turn_store=turn_store,
    )

    return AgentPipeline(
        agent=agent,
        context_manager=InMemoryContextManager(),
        tool_manager=tool_manager,
        input_adapter=_InputAdapter(),
        output_adapter=output_adapter,
        sanitizer=None,
        turn_store=turn_store,
        runtime_services=runtime_services,
        command_processor=command_processor,
        user_interface=IMUserInterface(output_adapter=output_adapter),
    )


async def _assert_one_suspended_snapshot(turn_store: InMemoryTurnStateStore, session_id: str):
    """Exactly one SUSPENDED + TOOL_APPROVAL_REQUIRED snapshot for the session."""
    snapshots = await turn_store.list_active_turns(
        StateQueryScope(
            session_id=session_id,
            phase=TurnPhase.SUSPENDED,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        )
    )
    assert len(snapshots) == 1, f"expected 1 suspended snapshot, got {len(snapshots)}"
    return snapshots[0]


async def _assert_no_suspended_snapshot(turn_store: InMemoryTurnStateStore, session_id: str):
    snapshots = await turn_store.list_active_turns(
        StateQueryScope(
            session_id=session_id,
            phase=TurnPhase.SUSPENDED,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        )
    )
    assert snapshots == [], f"expected no suspended snapshot, got {len(snapshots)}"


# ==========================================================================
# Test 1 — webui decision path (PRIMARY)
# ==========================================================================


@pytest.mark.asyncio
async def test_dangerous_tool_suspends_then_webui_decision_resumes(tmp_path: Path) -> None:
    """DANGEROUS tool -> suspend -> webui decision -> resume -> cleanup."""
    pipeline, _provider, output_adapter, turn_store, recorded = _build_pipeline(tmp_path=tmp_path)
    session = SessionInfo.from_str("s1.main")
    session_id = session.session_id

    # --- Step 1: dangerous tool call -> suspend ---
    result = await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    )

    # GraphInterrupt is caught inside TurnRunner.execute_turn -> returns None.
    assert result is None, "suspended turn must return None (GraphInterrupt swallowed)"
    snapshot = await _assert_one_suspended_snapshot(turn_store, session_id)

    # Structured approval prompt was rendered via the UI path.
    approval_msgs = [m for m in output_adapter.sent if m.message_type == "approval_request"]
    assert approval_msgs, "an approval_request OutputMessage must be rendered on suspend"
    rendered = approval_msgs[0]
    assert rendered.metadata["approval"]["tool_call_id"] == "c1"
    assert rendered.metadata["approval"]["tool_name"] == "write_file"

    # Tool did NOT execute while suspended.
    assert recorded == [], "tool must not execute before approval"

    # --- Step 2: webui decision -> resume -> execute -> cleanup ---
    decision_msg = InputMessage(
        content="",
        session=session,
        approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
    )
    resume_result = await pipeline._process_message(decision_msg)

    assert resume_result is not None, "resumed turn must complete with an AgentResult"
    # Tool executed exactly once with the suspended arguments.
    assert recorded == [("/etc/passwd", "x")], f"tool should execute after allow, got {recorded}"

    # Snapshot was DELETED from the store (cleanup on successful resume).
    await _assert_no_suspended_snapshot(turn_store, session_id)
    # And the specific snapshot identity is gone.
    assert await turn_store.load_turn(snapshot.identity) is None


# ==========================================================================
# Test 2 — IM /approve path
# ==========================================================================


@pytest.mark.asyncio
async def test_im_approve_command_resumes_suspended_turn(tmp_path: Path) -> None:
    """IM ``/approve`` -> command processor -> same resume branch as webui."""
    pipeline, _provider, output_adapter, turn_store, recorded = _build_pipeline(
        tmp_path=tmp_path,
        command_processor=SlashCommandProcessor.default(),
    )
    session = SessionInfo.from_str("s1.main")
    session_id = session.session_id

    # --- Suspend first ---
    result = await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    )
    assert result is None
    snapshot = await _assert_one_suspended_snapshot(turn_store, session_id)
    assert recorded == []

    # --- IM /approve -> command processor turns it into approval_action=ALLOW ---
    approve_msg = InputMessage(content="/approve", session=session)
    resume_result = await pipeline._process_message(approve_msg)

    assert resume_result is not None, "/approve must resume and complete the turn"
    assert recorded == [("/etc/passwd", "x")], f"tool should execute after /approve, got {recorded}"
    # Snapshot cleaned up.
    assert await turn_store.load_turn(snapshot.identity) is None
    await _assert_no_suspended_snapshot(turn_store, session_id)


# ==========================================================================
# Test 3 — path tiering + default-off
# ==========================================================================


@pytest.mark.asyncio
async def test_in_project_path_is_auto_allowed_no_suspend(tmp_path: Path) -> None:
    """In-project path (``./*``) -> NORMAL -> tool runs immediately, no suspend."""
    output_adapter = _RecordingOutputAdapter()
    turn_store = InMemoryTurnStateStore()
    recorded: list[tuple[str, str]] = []

    # Provider emits an IN-PROJECT path -> matches ``./*`` anchored at tmp_path.
    provider = _Provider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write_file",
                        arguments={"path": "./inside.txt", "content": "ok"},
                        call_id="c1",
                    ),
                ],
            ),
            LLMResponse(content="done"),
        ]
    )
    agent = ReActAgent(provider)
    pipeline = _build_pipeline_with_agent(
        tmp_path=tmp_path,
        agent=agent,
        output_adapter=output_adapter,
        turn_store=turn_store,
        recorded=recorded,
        enabled=True,
        allowed_paths=["./*"],
    )
    session = SessionInfo.from_str("s1.main")

    result = await pipeline._process_message(
        InputMessage(content="write inside", session=session)
    )

    assert result is not None, "in-project path must NOT suspend"
    assert recorded == [("./inside.txt", "ok")], f"in-project tool should run, got {recorded}"
    await _assert_no_suspended_snapshot(turn_store, session.session_id)
    # No approval prompt rendered.
    assert not [m for m in output_adapter.sent if m.message_type == "approval_request"]


@pytest.mark.asyncio
async def test_default_off_no_suspend_even_for_dangerous_path(tmp_path: Path) -> None:
    """``ApprovalConfig(enabled=False)`` -> build_approval_runtime returns None
    -> no classifier wired -> even ``/etc/passwd`` executes without suspend."""
    output_adapter = _RecordingOutputAdapter()
    turn_store = InMemoryTurnStateStore()
    recorded: list[tuple[str, str]] = []

    provider = _Provider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write_file",
                        arguments={"path": "/etc/passwd", "content": "x"},
                        call_id="c1",
                    ),
                ],
            ),
            LLMResponse(content="done"),
        ]
    )
    agent = ReActAgent(provider)
    pipeline = _build_pipeline_with_agent(
        tmp_path=tmp_path,
        agent=agent,
        output_adapter=output_adapter,
        turn_store=turn_store,
        recorded=recorded,
        enabled=False,  # default-off contract
        allowed_paths=["./*"],
    )

    # Sanity: the factory returned None, so runtime_services has no approval.
    assert pipeline.runtime_services is not None
    assert pipeline.runtime_services.approval is None, (
        "build_approval_runtime must return None when enabled=False"
    )

    session = SessionInfo.from_str("s1.main")
    result = await pipeline._process_message(
        InputMessage(content="write anywhere", session=session)
    )

    assert result is not None, "default-off must NOT suspend even for /etc/passwd"
    assert recorded == [("/etc/passwd", "x")], f"tool should run when off, got {recorded}"
    await _assert_no_suspended_snapshot(turn_store, session.session_id)
    assert not [m for m in output_adapter.sent if m.message_type == "approval_request"]
