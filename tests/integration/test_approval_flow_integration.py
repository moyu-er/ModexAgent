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

The only fakes are a scripted LLM provider and a recording ``write`` tool.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

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
from tests.unit.pipeline._helpers import _make_react_pipeline
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


class _ValidatingProvider:
    """Scripted LLM that enforces the OpenAI message-ordering invariant.

    Reproduces the production 400 ("an assistant message with 'tool_calls'
    must be followed by tool messages responding to each 'tool_call_id'")
    at test time: on every ``chat`` it asserts that each assistant message
    carrying ``tool_calls`` is immediately followed by matching tool
    messages. The plain :class:`_Provider` accepts any history, so it could
    not catch a resume that re-enters at the LLM node with a stale
    tool_calls message and no tool results.
    """

    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.calls = 0
        self.received: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.received.append([dict(m) for m in messages])
        self._assert_tool_messages_follow_tool_calls(messages)
        resp = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return resp

    @staticmethod
    def _assert_tool_messages_follow_tool_calls(messages) -> None:
        msgs = list(messages)
        i = 0
        while i < len(msgs):
            m = msgs[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                expected = {tc.get("id") for tc in m["tool_calls"]}
                j = i + 1
                seen = set()
                while j < len(msgs) and msgs[j].get("role") == "tool":
                    tid = msgs[j].get("tool_call_id")
                    if tid:
                        seen.add(tid)
                    j += 1
                missing = expected - seen
                assert not missing, (
                    f"LLM received assistant tool_calls {expected} not followed "
                    f"by tool messages for {missing}. Messages: {msgs}"
                )
                i = j
            else:
                i += 1


class _WriteFileTool(Tool):
    """Recording ``write`` tool — matches the production tool name.

    The approval classifier does a LITERAL ``config.tools.get(tool_name)``
    lookup, so this name, the ``ToolCall.tool_name`` the provider emits, and
    the approval-config key must all be the same string. Execution just needs
    to succeed and record its arguments.
    """

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

    # Script: call 1 emits the dangerous write tool call; call 2 returns
    # a plain content response so the resumed turn completes. Tests that need
    # a different script build their own provider and pass a fresh agent via
    # _build_pipeline_with_agent.
    provider = _Provider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
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
        tools={"write": ToolApprovalEntry(allowed_paths=allowed_paths or ["./*"])},
    )
    approval_runtime = build_approval_runtime(approval_cfg, project_root=tmp_path)

    runtime_services = AgentRuntimeServices(
        approval=approval_runtime,
        turn_store=turn_store,
    )

    pipeline = _make_react_pipeline(
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
    governance: Any | None = None,
    context_manager: Any | None = None,
    agent_descriptor: Any | None = None,
) -> AgentPipeline:
    """Build a pipeline around an already-constructed agent + stores.

    Used by path-tiering / default-off tests that feed a different provider
    script. ``governance`` wires a ContextGovernance chain (production wires
    tool-chain repair + lossy compaction); None leaves governance off.
    ``context_manager`` defaults to InMemoryContextManager; pass a production
    MemorySystemContextManager to exercise the real message store layer.
    ``agent_descriptor`` reproduces production wiring: when set, the snapshot's
    ``identity.agent_id`` becomes ``descriptor.address.name`` (e.g. "main"),
    which differs from ``agent.name`` ("ReActAgent") — the exact mismatch that
    broke resume before the load_pending scope fix.
    """
    tool_manager = InMemoryToolManager()
    tool_manager.register(_WriteFileTool(recorded))

    approval_cfg = ApprovalConfig(
        enabled=enabled,
        tools={"write": ToolApprovalEntry(allowed_paths=allowed_paths or ["./*"])},
    )
    approval_runtime = build_approval_runtime(approval_cfg, project_root=tmp_path)

    runtime_services = AgentRuntimeServices(
        approval=approval_runtime,
        turn_store=turn_store,
        governance=governance,
    )

    return _make_react_pipeline(
        agent=agent,
        context_manager=context_manager or InMemoryContextManager(),
        tool_manager=tool_manager,
        input_adapter=_InputAdapter(),
        output_adapter=output_adapter,
        sanitizer=None,
        turn_store=turn_store,
        runtime_services=runtime_services,
        command_processor=command_processor,
        user_interface=IMUserInterface(output_adapter=output_adapter),
        agent_descriptor=agent_descriptor,
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
    assert rendered.metadata["approval"]["tool_name"] == "write"

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
                        tool_name="write",
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
                        tool_name="write",
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
    _rs = pipeline._turn_runner._builder._runtime_services  # type: ignore[attr-defined]
    assert _rs is not None
    assert _rs.approval is None, (
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


# ==========================================================================
# Test 4 — resume must feed the LLM a well-formed history
# ==========================================================================


@pytest.mark.asyncio
async def test_resume_feeds_llm_well_formed_history(tmp_path: Path) -> None:
    """After approval, the resumed turn must execute the tool (producing the
    tool-result message) BEFORE re-calling the LLM.

    The plain ``_Provider`` accepts any history, so it cannot catch a resume
    that re-enters at the LLM node with the suspended assistant ``tool_calls``
    message but no matching tool messages — which is exactly the production
    400 ("an assistant message with 'tool_calls' must be followed by tool
    messages"). This test uses a provider that enforces the invariant.
    """
    output_adapter = _RecordingOutputAdapter()
    turn_store = InMemoryTurnStateStore()
    recorded: list[tuple[str, str]] = []

    provider = _ValidatingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
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
        enabled=True,
        allowed_paths=["./*"],
    )
    session = SessionInfo.from_str("s1.main")

    # Step 1: dangerous tool call -> suspend.
    assert await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    ) is None
    await _assert_one_suspended_snapshot(turn_store, session.session_id)

    # Step 2: approval -> resume. The validating provider asserts on the 2nd
    # call that the assistant tool_calls message is followed by tool messages.
    resume_result = await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
        )
    )
    assert resume_result is not None
    assert provider.calls == 2, f"LLM should be called twice, got {provider.calls}"
    assert recorded == [("/etc/passwd", "x")]


@pytest.mark.asyncio
async def test_resume_with_tool_chain_governance_feeds_llm_well_formed_history(
    tmp_path: Path,
) -> None:
    """Production wires ``ToolChainRepairGovernance`` (tool-chain sanitizer) on
    every LLM call. The sanitizer must NOT strip the tool-result message that
    the resumed tool node just produced, or the LLM is called with a dangling
    assistant ``tool_calls`` and no matching tool message -> 400."""
    from modex_agent.memory.context_governance import (
        CompositeGovernance,
        LossyContentCompactionGovernance,
        ToolChainRepairGovernance,
    )

    governance = CompositeGovernance(
        [
            LossyContentCompactionGovernance(),
            ToolChainRepairGovernance(),
        ]
    )
    output_adapter = _RecordingOutputAdapter()
    turn_store = InMemoryTurnStateStore()
    recorded: list[tuple[str, str]] = []

    provider = _ValidatingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
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
        enabled=True,
        allowed_paths=["./*"],
        governance=governance,
    )
    session = SessionInfo.from_str("s1.main")

    assert await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    ) is None
    await _assert_one_suspended_snapshot(turn_store, session.session_id)

    resume_result = await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
        )
    )
    assert resume_result is not None
    assert provider.calls == 2, f"LLM should be called twice, got {provider.calls}"
    assert recorded == [("/etc/passwd", "x")]


@pytest.mark.asyncio
async def test_post_construction_governance_mirrors_and_backfills_dangling_toolcalls(
    tmp_path: Path,
) -> None:
    """bug #1: governance assigned POST-construction (the pool_builder.py:930
    ``pipeline.governance = create_governance(...)`` pattern) must reach the
    per-turn runtime via the mirror setter, and the sanitizer must backfill a
    dangling assistant tool_call so the provider never sees tool_calls without
    matching tool results (400).

    Before the fix: ``governance`` was a plain attribute with no setter, so the
    post-construction assignment never reached ``TurnContextBuilder._governance``
    and the sanitizer never ran — a dangling tool_call went straight to the LLM.
    """
    from modex_agent.core.context import InMemoryContextManager
    from modex_agent.memory.context_governance import (
        CompositeGovernance,
        LossyContentCompactionGovernance,
        ToolChainRepairGovernance,
    )

    output_adapter = _RecordingOutputAdapter()
    turn_store = InMemoryTurnStateStore()
    recorded: list[tuple[str, str]] = []
    provider = _ValidatingProvider([LLMResponse(content="done")])
    agent = ReActAgent(provider)

    # Pre-pollute history with a dangling assistant tool_call (no tool result).
    ctx_mgr = InMemoryContextManager()
    session = SessionInfo.from_str("s1.main")
    state = await ctx_mgr.load(session.session_id)
    await state.history.append({"role": "user", "content": "earlier"})
    await state.history.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "dangling", "function": {"name": "write"}}],
        }
    )

    # Construct with governance=None, THEN assign post-construction (mirror path).
    pipeline = _build_pipeline_with_agent(
        tmp_path=tmp_path,
        agent=agent,
        output_adapter=output_adapter,
        turn_store=turn_store,
        recorded=recorded,
        governance=None,
        context_manager=ctx_mgr,
    )
    governance = CompositeGovernance(
        [LossyContentCompactionGovernance(), ToolChainRepairGovernance()]
    )
    pipeline._turn_runner._builder._governance = governance  # type: ignore[attr-defined]

    # The mirror setter must propagate to the builder (the bug was that it didn't).
    assert pipeline._turn_runner._builder._governance  # type: ignore[attr-defined] is governance

    result = await pipeline._process_message(
        InputMessage(content="continue", session=session)
    )
    assert result is not None
    assert provider.calls >= 1
    # _ValidatingProvider.chat asserts every assistant tool_call is followed by a
    # matching tool message. If the mirror/backfill failed, the dangling
    # tool_call reaches the LLM and the assertion raises (the production 400).


@pytest.mark.asyncio
async def test_snapshot_and_resume_connect_across_different_agent_ids(
    tmp_path: Path,
) -> None:
    """Snapshot generation and resume are two independent flows that must connect.

    Flow A (snapshot): during turn execution ``identity.agent_id`` comes from
    ``agent_descriptor.address.name`` ("main" in production).
    Flow B (resume): ``load_pending`` queries by session_id; ``agent.name`` is
    the hardcoded class constant "ReActAgent".

    These two agent_ids differ. ``load_pending`` must connect them by session_id,
    NOT by agent.name — using agent.name as a scope silently mismatches and the
    approve click does nothing (the production bug). This test reproduces the
    real wiring (agent_descriptor set) where the previous tests' blind spot hid.
    """
    from modex_agent.multi_agent import AgentDescriptor
    from modex_agent.multi_agent.address import AgentAddress

    descriptor = AgentDescriptor(address=AgentAddress(name="main"), system_prompt_template="x")
    provider = _ValidatingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
                        arguments={"path": "/etc/x", "content": "y"},
                        call_id="c1",
                    ),
                ],
            ),
            LLMResponse(content="done"),
        ]
    )
    output_adapter = _RecordingOutputAdapter()
    turn_store = InMemoryTurnStateStore()
    recorded: list[tuple[str, str]] = []
    pipeline = _build_pipeline_with_agent(
        tmp_path=tmp_path,
        agent=ReActAgent(provider),
        output_adapter=output_adapter,
        turn_store=turn_store,
        recorded=recorded,
        agent_descriptor=descriptor,
    )
    session = SessionInfo.from_str("s1.main")

    # Flow A: snapshot is stored with agent_id from the descriptor ("main").
    assert await pipeline._process_message(
        InputMessage(content="do it", session=session)
    ) is None
    snap = await _assert_one_suspended_snapshot(turn_store, session.session_id)
    assert snap.identity.agent_id == "main"
    assert pipeline.agent.name == "ReActAgent"  # differs from snapshot agent_id

    # Flow B: resume must find the snapshot by session_id and execute the tool.
    result = await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
        )
    )
    assert result is not None
    assert recorded == [("/etc/x", "y")]
    await _assert_no_suspended_snapshot(turn_store, session.session_id)


@pytest.mark.asyncio
async def test_resume_isolated_by_session_id_no_cross_contamination(
    tmp_path: Path,
) -> None:
    """With agent_id removed from the scope, session_id remains the partition:
    an approve for session B must NOT resume session A's suspended snapshot.
    Locks the isolation guarantee so the scope fix can't be regressed into
    cross-session leakage."""
    from modex_agent.multi_agent import AgentDescriptor
    from modex_agent.multi_agent.address import AgentAddress

    descriptor = AgentDescriptor(address=AgentAddress(name="main"), system_prompt_template="x")
    provider = _ValidatingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
                        arguments={"path": "/etc/a", "content": "1"},
                        call_id="ca",
                    ),
                ],
            ),
            LLMResponse(content="done"),
        ]
    )
    output_adapter = _RecordingOutputAdapter()
    turn_store = InMemoryTurnStateStore()
    recorded: list[tuple[str, str]] = []
    pipeline = _build_pipeline_with_agent(
        tmp_path=tmp_path,
        agent=ReActAgent(provider),
        output_adapter=output_adapter,
        turn_store=turn_store,
        recorded=recorded,
        agent_descriptor=descriptor,
    )
    sess_a = SessionInfo.from_str("s1.main")
    sess_b = SessionInfo.from_str("s2.main")

    # Session A suspends a write.
    assert await pipeline._process_message(
        InputMessage(content="do a", session=sess_a)
    ) is None
    await _assert_one_suspended_snapshot(turn_store, sess_a.session_id)

    # Session B approves (different session_id) — must not touch A's snapshot.
    await pipeline._process_message(
        InputMessage(
            content="",
            session=sess_b,
            approval_decision=ApprovalDecisionInput("ca", ApprovalAction.ALLOW),
        )
    )
    assert recorded == [], "session B approve must not execute session A's tool"
    await _assert_one_suspended_snapshot(turn_store, sess_a.session_id)


@pytest.mark.asyncio
async def test_webui_approval_decision_not_persisted_as_empty_user_message(
    tmp_path: Path,
) -> None:
    """A webui approval decision must trigger the resume flow, NOT be persisted
    as an empty user message into conversation history.

    This is the bug #2 symptom: when approval_decision was lost in transit,
    build_turn_request fell back to the plain branch (content="" +
    append_user_message=True) and every approve/deny click polluted memory with
    an empty role=user message. With the decision carried through, the
    short-circuit branch sets append_user_message=False and resume runs
    instead — no empty user is stored.
    """
    from modex_agent.core.types import MessageRole

    pipeline, provider, output_adapter, turn_store, recorded = _build_pipeline(tmp_path=tmp_path)
    session = SessionInfo.from_str("s1.main")

    # turn 1: dangerous write -> suspend
    assert await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    ) is None
    await _assert_one_suspended_snapshot(turn_store, session.session_id)

    # turn 2: webui approve (content="" — the decision rides on approval_decision)
    resume_result = await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
        )
    )
    assert resume_result is not None

    # History must contain NO empty user message (the decision leaking in).
    state = await pipeline.context_manager.load(session.session_id)
    history = await state.history.to_list()
    empty_users = [
        m
        for m in history
        if m.get("role") == str(MessageRole.USER)
        and not (m.get("content") or "").strip()
    ]
    assert empty_users == [], f"approval decision leaked as empty user message: {history}"
    # The original user request is preserved (resume saves the restored turn).
    user_contents = [
        (m.get("content") or "")
        for m in history
        if m.get("role") == str(MessageRole.USER)
    ]
    assert any("write secrets" in c for c in user_contents), (
        f"original user request missing from history: {user_contents}"
    )


@pytest.mark.asyncio
async def test_resume_with_file_turn_store_feeds_llm_well_formed_history(
    tmp_path: Path,
) -> None:
    """Same as above but with the REAL ``JsonFileTurnStateStore``.

    The InMemory store retains the live ``ReActTurnState`` object, masking any
    snapshot serialization round-trip defect. Production persists the snapshot
    to JSON and reconstructs the state via the codec — if ``phase`` or
    ``current_node`` does not survive the round-trip, the resumed graph
    re-enters at the LLM node (not the TOOL node) and the LLM is called with
    the suspended assistant ``tool_calls`` message but no tool results -> 400.
    """
    from modex_agent.agents.react.state import ReActRuntimeStateCodec
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry
    from modex_agent.runtime.enums import AgentKind
    from modex_agent.runtime.store import JsonFileTurnStateStore

    codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
    turn_store = JsonFileTurnStateStore(tmp_path / "turns", codec_registry)
    output_adapter = _RecordingOutputAdapter()
    recorded: list[tuple[str, str]] = []

    provider = _ValidatingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
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
        turn_store=turn_store,  # type: ignore[arg-type]
        recorded=recorded,
        enabled=True,
        allowed_paths=["./*"],
    )
    session = SessionInfo.from_str("s1.main")

    # Step 1: dangerous tool call -> suspend (snapshot persisted to JSON file).
    assert await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    ) is None
    await _assert_one_suspended_snapshot(turn_store, session.session_id)  # type: ignore[arg-type]

    # Step 2: approval -> resume. Validating provider asserts well-formed history.
    resume_result = await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
        )
    )
    assert resume_result is not None
    assert provider.calls == 2, f"LLM should be called twice, got {provider.calls}"
    assert recorded == [("/etc/passwd", "x")]


@pytest.mark.asyncio
async def test_resume_with_production_memory_cm_feeds_llm_well_formed_history(
    tmp_path: Path,
) -> None:
    """DECISIVE reproduction: use the REAL production context manager
    (``MemorySystemContextManager`` + ``ScopedMessageHistory``), whose ``append``
    writes through to a message store and runs ``cleanup_session`` after every
    append, and whose ``to_list`` re-reads via ``get_recent_messages``.

    Every prior test used ``InMemoryContextManager`` (shared in-memory object,
    no store round-trip, no cleanup) — so none could catch a production-only
    defect where the message store drops/mangles the tool-result message
    produced by the resumed tool node, leaving a dangling assistant
    ``tool_calls`` -> 400.
    """
    from modex_agent.memory.default_system import DefaultMemorySystem
    from modex_agent.memory.layers.factory import MemoryLayerFactory
    from modex_agent.memory.registry import DefaultMemoryStoreRegistry
    from modex_agent.memory.system import MemorySystemContextManager

    registry = DefaultMemoryStoreRegistry(tmp_path)
    memory_system = DefaultMemorySystem(
        layer_set=MemoryLayerFactory.single_user(registry=registry),
        store_registry=registry,
    )
    await memory_system.initialize()
    ctx_mgr = MemorySystemContextManager(memory_system)

    output_adapter = _RecordingOutputAdapter()
    turn_store = InMemoryTurnStateStore()
    recorded: list[tuple[str, str]] = []

    provider = _ValidatingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
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
        enabled=True,
        allowed_paths=["./*"],
        context_manager=ctx_mgr,
    )
    session = SessionInfo.from_str("s1.main")

    assert await pipeline._process_message(
        InputMessage(content="write secrets", session=session)
    ) is None
    await _assert_one_suspended_snapshot(turn_store, session.session_id)

    resume_result = await pipeline._process_message(
        InputMessage(
            content="",
            session=session,
            approval_decision=ApprovalDecisionInput("c1", ApprovalAction.ALLOW),
        )
    )
    assert resume_result is not None
    assert provider.calls == 2, f"LLM should be called twice, got {provider.calls}"
    assert recorded == [("/etc/passwd", "x")]
    await memory_system.close()
