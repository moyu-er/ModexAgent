"""Human-approved marker passthrough — the 批了白批 close (Ticket 03).

Coverage per unified-security Ticket 03 / PRD 验收标准 2:

- WRITE: ``ApprovalResumer.apply_resume`` on an ALLOW decision records
  ``custom[TurnCustomKey.HUMAN_APPROVED_CALLS][tool_call_id] = anchor``
  into the snapshot payload (anchored against the live workspace root).
- READ: ``SandboxGuardInterceptor`` waives a BOUNDARY denial iff the
  call's marker anchor matches (same ``approval_anchor`` derivation,
  same live root); mismatch (workspace switched while suspended) still
  denies; HARDLINE categories never honor the marker.
- END-TO-END: classify (DANGEROUS card) → approve via the resumer →
  restore state → the interceptor that would have denied the exact
  boundary-outside call now executes it (white-approval closed loop).
- NO MARKER / DENY path: behavior identical to pre-Ticket-03.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modex_agent.agents.react.state import (
    ReActNode,
    ReActSnapshotPolicy,
    ReActTurnState,
)
from modex_agent.approval.constants import ApprovalTier
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ToolCall
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolResult
from modex_agent.interceptor.abc import ToolCallContext
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.models import ApprovalAction
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.runtime.enums import (
    AgentKind,
    ApprovalSubjectType,
    SnapshotReason,
    TurnCustomKey,
    TurnPhase,
)
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ToolArguments,
    TurnIdentity,
    TurnSnapshot,
)
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.sandbox.decision import GuardCategory, SecurityDecisionService
from modex_agent.sandbox.interceptor import SandboxGuardInterceptor
from modex_agent.sandbox.runtime import ResolvedSandbox, SandboxRuntime
from modex_agent.sandbox.settings import (
    SandboxBackend,
    SandboxPolicy,
    SandboxSettings,
)
from modex_agent.sandbox.types import EnforcementLevel
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.workspace.boundary import canonicalize_path
from modex_agent.workspace.runtime import bind_workspace_root

WS = Path("/ws/project")
OTHER_WS = Path("/other/workspace")


class _FixedRoot(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


class _SwitchableRoot(WorkspaceRootProvider):
    def __init__(self) -> None:
        self._root = WS

    def current(self) -> Path:
        return self._root

    def switch(self, root: Path) -> None:
        self._root = root


class _FakeRuntime(SandboxRuntime):
    def __init__(self) -> None:
        self._resolved = ResolvedSandbox(
            backend=SandboxBackend.HOST,
            enforcement=EnforcementLevel.NONE,
            shell_argv=[],
            one_shot_command_argv_prefix=[],
        )

    async def resolve(
        self, settings: SandboxSettings, workspace_root: Path
    ) -> ResolvedSandbox:
        return self._resolved


def _settings(policy: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE) -> SandboxSettings:
    return SandboxSettings.model_validate({"backend": "host", "policy": policy})


def _service(
    root: WorkspaceRootProvider, policy: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE
) -> SecurityDecisionService:
    return SecurityDecisionService(
        settings=_settings(policy), workspace_root_provider=root
    )


def _guard(
    root: WorkspaceRootProvider, policy: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE
) -> SandboxGuardInterceptor:
    return SandboxGuardInterceptor(
        settings=_settings(policy),
        runtime=_FakeRuntime(),
        workspace_root_provider=root,
        decision=_service(root, policy),
    )


def _turn_state() -> ReActTurnState:
    return ReActTurnState(
        identity=TurnIdentity(
            agent_id="agent",
            session=SessionInfo.from_str("s1.main"),
            turn_id="t1",
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )


def _agent_ctx(state: ReActTurnState) -> AgentContext:
    ctx = AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("s1.main"),
    )
    ctx.runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return ctx


def _call(
    tool_name: str,
    arguments: dict[str, str],
    call_id: str,
    turn_state: ReActTurnState | None = None,
) -> ToolCallContext:
    return ToolCallContext(
        tool_call=ToolCall(tool_name=tool_name, arguments=arguments, call_id=call_id),
        tool_name=tool_name,
        arguments=arguments,
        session_id="s1",
        turn_id="t1",
        turn_state=turn_state,
    )


def _boundary_call(state: ReActTurnState, call_id: str = "c1") -> ToolCallContext:
    # /etc is outside WS under workspace-write → BOUNDARY.
    return _call("write", {"path": "/etc/hosts", "content": "x"}, call_id, state)


def _hardline_call(state: ReActTurnState, call_id: str = "call-1") -> ToolCallContext:
    # fork bomb → DENY_RULE (HARDLINE).
    return _call("bash", {"command": ":(){ :|:& };:"}, call_id, state)


async def _pass(guard: SandboxGuardInterceptor, call: ToolCallContext) -> ToolResult:
    """Drive one interception; the marker lives on call.turn_state (the
    production seam) with ctx.runtime.state as fallback."""
    carried = call.turn_state
    state = carried if isinstance(carried, ReActTurnState) else _turn_state()
    ctx = _agent_ctx(state)

    async def _next() -> ToolResult:
        return ToolResult.from_text(call.tool_name, "executed")

    return await guard.around_tool_call(ctx, call, _next)


# ---------------------------------------------------------------------------
# Marker WRITE — ApprovalResumer.apply_resume on ALLOW
# ---------------------------------------------------------------------------


def _approval_snapshot(
    arguments: dict[str, str], *, tool_name: str = "write"
) -> TurnSnapshot:
    identity = TurnIdentity(
        agent_id="agent",
        session=SessionInfo.from_str("s1"),
        turn_id="t1",
    )
    request = ApprovalRequestState(
        request_id="r1",
        approval_id="ap1",
        tool_call_id="c1",
        tool_name=tool_name,
        arguments=ToolArguments(values=arguments),
        tier=ApprovalTier.DANGEROUS,
        iteration=1,
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        current_node=ReActNode.TOOL,
        approval=ApprovalTransaction(
            approval_id="ap1",
            turn_id="t1",
            subject_type=ApprovalSubjectType.TOOL_BATCH,
            subject_ids=["batch1"],
            requests=[request],
        ),
    )
    state.custom[TurnCustomKey.TURN_UUID] = "turn-uuid-1"
    return ReActSnapshotPolicy().capture(state, SnapshotReason.TOOL_APPROVAL_REQUIRED)


class _FakeAgent:
    def __init__(self, name: str = "agent") -> None:
        self.name = name


def _resumer() -> ApprovalResumer:
    return ApprovalResumer(
        agent=_FakeAgent(),  # type: ignore[arg-type]
        turn_store=None,
        user_interface=None,
    )


def _payload_custom(snapshot: TurnSnapshot) -> dict[str, Any]:
    custom = dict(snapshot.state_payload).get("custom")
    assert isinstance(custom, dict)
    return custom


class TestMarkerWrite:
    async def test_allow_writes_marker_anchored_to_live_root(self) -> None:
        with bind_workspace_root(WS):
            snapshot = _approval_snapshot({"path": "notes/todo.md", "content": "x"})
            result = await _resumer().apply_resume(
                snapshot,
                action=ApprovalAction.ALLOW,
                session_id="s1",
                pool_data=None,
                agent_context=_agent_ctx(_turn_state()),
            )
        assert result is None  # no turn_store → non-resuming, but the marker persists
        marked = _payload_custom(snapshot).get(TurnCustomKey.HUMAN_APPROVED_CALLS.value)
        assert marked == {"c1": str((WS / "notes" / "todo.md").resolve(strict=False))}

    async def test_absolute_path_anchors_verbatim(self) -> None:
        with bind_workspace_root(WS):
            snapshot = _approval_snapshot({"path": "/etc/hosts", "content": "x"})
            await _resumer().apply_resume(
                snapshot,
                action=ApprovalAction.ALLOW,
                session_id="s1",
                pool_data=None,
                agent_context=_agent_ctx(_turn_state()),
            )
        marked = _payload_custom(snapshot).get(TurnCustomKey.HUMAN_APPROVED_CALLS.value)
        assert marked == {"c1": str(Path("/etc/hosts").resolve(strict=False))}

    async def test_deny_writes_no_marker(self) -> None:
        snapshot = _approval_snapshot({"path": "/etc/hosts", "content": "x"})
        await _resumer().apply_resume(
            snapshot,
            action=ApprovalAction.DENY,
            session_id="s1",
            pool_data=None,
            agent_context=_agent_ctx(_turn_state()),
        )
        assert TurnCustomKey.HUMAN_APPROVED_CALLS.value not in _payload_custom(snapshot)

    async def test_marker_survives_snapshot_round_trip(self) -> None:
        snapshot = _approval_snapshot({"path": "notes/a.md", "content": "x"})
        with bind_workspace_root(WS):
            await _resumer().apply_resume(
                snapshot,
                action=ApprovalAction.ALLOW,
                session_id="s1",
                pool_data=None,
                agent_context=_agent_ctx(_turn_state()),
            )
        state = ReActSnapshotPolicy.state_from_snapshot(snapshot)
        marked = state.custom[TurnCustomKey.HUMAN_APPROVED_CALLS]
        assert marked == {"c1": str((WS / "notes" / "a.md").resolve(strict=False))}


# ---------------------------------------------------------------------------
# Marker READ — interceptor backstop
# ---------------------------------------------------------------------------


def _mark(state: ReActTurnState, call_id: str, anchor: str) -> None:
    marked = state.custom.get(TurnCustomKey.HUMAN_APPROVED_CALLS, {})
    marked[call_id] = anchor
    state.custom[TurnCustomKey.HUMAN_APPROVED_CALLS] = marked


class TestInterceptorBackstop:
    async def test_matching_marker_waives_boundary_denial(self) -> None:
        guard = _guard(_FixedRoot(WS))
        state = _turn_state()
        _mark(state, "c1", str(Path("/etc/hosts").resolve(strict=False)))
        result = await _pass(guard, _boundary_call(state))
        assert result.error is None
        assert result.message_content() == "executed"

    async def test_anchor_mismatch_still_denies(self) -> None:
        guard = _guard(_FixedRoot(WS))
        state = _turn_state()
        # marker anchored to a DIFFERENT path than the one being executed
        _mark(state, "c1", str(Path("/etc/passwd")))
        result = await _pass(guard, _boundary_call(state))
        assert result.error is not None
        assert "Sandbox policy denied" in result.error

    async def test_workspace_switch_while_suspended_anchor_diverges(self) -> None:
        # TOCTOU anchor divergence: the same relative argument re-anchors
        # under a switched live root, so the approval-time and
        # execution-time anchors differ — the marker no longer matches.
        # (The BOUNDARY verdict itself keys on the service's fixed policy
        # root; a relative in-boundary path stays CLEAN under it, so the
        # intercept-level denial only exists once the boundary actually
        # fires — what this test pins is the anchor divergence the waive
        # check would see.)
        from modex_agent.sandbox.decision import approval_anchor

        root = _SwitchableRoot()
        guard = _guard(root)
        state = _turn_state()
        approved = approval_anchor("write", {"path": "notes/a.md"}, WS)
        assert approved is not None
        _mark(state, "call-1", approved)
        root.switch(OTHER_WS)
        at_execution = approval_anchor(
            "write", {"path": "notes/a.md"}, guard._root_provider.current()
        )
        assert at_execution is not None
        assert at_execution != approved  # the anchor moved with the root
        # and an absolute out-of-boundary argument still denies when the
        # marker does not match the executed argument
        _mark(state, "call-1", str(Path("/etc/passwd")))
        result = await _pass(guard, _boundary_call(state))
        assert result.error is not None
        assert "Sandbox policy denied" in result.error

    async def test_same_relative_path_same_root_passes(self) -> None:
        # The white-approval positive: approve + execute under the same
        # root — identical anchors.
        from modex_agent.sandbox.decision import approval_anchor

        guard = _guard(_FixedRoot(WS))
        state = _turn_state()
        anchor = approval_anchor("write", {"path": "notes/a.md"}, WS)
        assert anchor is not None
        _mark(state, "call-1", anchor)
        call = _call("write", {"path": "notes/a.md", "content": "x"}, "call-1", state)
        result = await _pass(guard, call)
        assert result.error is None

    async def test_hardline_deny_rule_never_honors_marker(self) -> None:
        guard = _guard(_FixedRoot(WS))
        state = _turn_state()
        _mark(state, "call-1", ":(){ :|:& };:")  # exact command approved
        result = await _pass(guard, _hardline_call(state))
        assert result.error is not None
        assert "Sandbox policy denied" in result.error

    async def test_hardline_ssrf_never_honors_marker(self) -> None:
        guard = _guard(_FixedRoot(WS))
        state = _turn_state()
        _mark(state, "call-1", "http://169.254.169.254/")
        call = _call("web_reader", {"url": "http://169.254.169.254/"}, "call-1", state)
        result = await _pass(guard, call)
        assert result.error is not None

    async def test_no_marker_keeps_pre_ticket_behavior(self) -> None:
        guard = _guard(_FixedRoot(WS))
        state = _turn_state()
        result = await _pass(guard, _boundary_call(state))
        assert result.error is not None
        assert "Sandbox policy denied" in result.error
        assert "/etc/hosts" in result.error

    async def test_marker_for_other_call_id_ignored(self) -> None:
        guard = _guard(_FixedRoot(WS))
        state = _turn_state()
        _mark(state, "other-call", str(Path("/etc/hosts")))
        result = await _pass(guard, _boundary_call(state))
        assert result.error is not None

    async def test_runtime_state_fallback_reads_marker(self) -> None:
        # call.turn_state unset → falls back to ctx.runtime.state.
        guard = _guard(_FixedRoot(WS))
        state = _turn_state()
        _mark(state, "c1", str(Path("/etc/hosts").resolve(strict=False)))
        call = _boundary_call(_turn_state())  # turn_state=None: not carried
        call = ToolCallContext(
            tool_call=call.tool_call,
            tool_name=call.tool_name,
            arguments=call.arguments,
            session_id=call.session_id,
            turn_id=call.turn_id,
        )
        ctx = _agent_ctx(state)  # runtime.state carries the marker

        async def _next() -> ToolResult:
            return ToolResult.from_text("write", "executed")

        result = await guard.around_tool_call(ctx, call, _next)
        assert result.error is None


# ---------------------------------------------------------------------------
# END-TO-END — classify → approve → execute (the white-approval loop)
# ---------------------------------------------------------------------------


class TestEndToEndWhiteApproval:
    async def test_classify_card_approve_execute_loop(self) -> None:
        """PRD 验收标准 2: outside-envelope path → DANGEROUS card →
        /approve → the guard that would deny now executes."""
        from modex_agent.approval.config import AgentApprovalConfig, ToolApprovalConfig
        from modex_agent.approval.runtime import (
            ApprovalRuntime,
            TieredToolApprovalClassifier,
        )
        from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher
        from modex_agent.sandbox.security_classifier import SecurityClassifier

        classifier = SecurityClassifier(
            decision=_service(_FixedRoot(WS)),
            inner=TieredToolApprovalClassifier(
                config=AgentApprovalConfig(
                    enabled=True,
                    tools={"write": ToolApprovalConfig(allowed_paths=["./*"])},
                ),
                argument_matcher=ArgumentMatcher(),
            ),
            escalate_enabled=True,
        )
        classify_ctx = _agent_ctx(_turn_state())
        assert classify_ctx.runtime is not None
        classify_ctx.runtime.services.approval = ApprovalRuntime(classifier=classifier)

        # 1. classify: absolute path outside the envelope → DANGEROUS
        tc = ToolCall(
            tool_name="write",
            arguments={"path": "/etc/hosts", "content": "x"},
            call_id="c1",
        )
        assert classifier.classify(tc, classify_ctx).tier is ApprovalTier.DANGEROUS

        # 2. suspend → snapshot → user approves via the resumer
        snapshot = _approval_snapshot({"path": "/etc/hosts", "content": "x"})
        with bind_workspace_root(WS):
            await _resumer().apply_resume(
                snapshot,
                action=ApprovalAction.ALLOW,
                session_id="s1",
                pool_data=None,
                agent_context=_agent_ctx(_turn_state()),
            )

        # 3. resume restores the state (marker rides along)
        state = ReActSnapshotPolicy.state_from_snapshot(snapshot)
        marked = state.custom[TurnCustomKey.HUMAN_APPROVED_CALLS]
        assert marked is not None
        assert marked["c1"] == str(Path("/etc/hosts").resolve(strict=False))

        # 4. the interceptor — which denied this exact call pre-approval —
        #    now executes it
        guard = _guard(_FixedRoot(WS))
        exec_ctx = _agent_ctx(state)
        call = _boundary_call(state)

        async def _next() -> ToolResult:
            return ToolResult.from_text("write", "written")

        result = await guard.around_tool_call(exec_ctx, call, _next)
        assert result.error is None
        assert result.message_content() == "written"

    async def test_unapproved_denied_by_guard(self) -> None:
        """The negative arm of 验收标准 2: no approval → DENIED."""
        guard = _guard(_FixedRoot(WS))
        state = _turn_state()  # no marker
        result = await _pass(guard, _boundary_call(state))
        assert result.error is not None


# ---------------------------------------------------------------------------
# Regression — verdict-delegation denial copy stays byte-identical
# ---------------------------------------------------------------------------


class TestDelegationRegression:
    async def test_read_only_write_denial_copy(self) -> None:
        guard = _guard(_FixedRoot(WS), policy=SandboxPolicy.READ_ONLY)
        state = _turn_state()
        result = await _pass(
            guard, _call("write", {"path": "a.py", "content": "x"}, "c1", state)
        )
        assert result.error == (
            "Sandbox policy denied: write tool 'write' refused — policy: read-only "
            "(every path is read-only)\n\n"
            "  target: a.py\n\n"
            "Request a sandbox policy change to enable writes."
        )

    async def test_file_boundary_denial_copy(self) -> None:
        guard = _guard(_FixedRoot(WS))
        state = _turn_state()
        result = await _pass(guard, _boundary_call(state))
        assert result.error is not None
        canonical_ws = str(canonicalize_path(WS))
        assert result.error == (
            "Sandbox policy denied: target outside the sandbox boundary "
            "(policy: workspace-write)\n\n"
            f"Path '/etc/hosts' is outside the allowed workspace '{canonical_ws}'\n\n"
            f"Allowed roots:\n  - {canonical_ws}\n\n"
            "Use paths within the allowed roots, or adjust writable_roots."
        )

    async def test_command_deny_copy_carries_reason(self) -> None:
        guard = _guard(_FixedRoot(WS))
        state = _turn_state()
        result = await _pass(guard, _hardline_call(state))
        assert result.error is not None
        assert result.error.startswith("Sandbox policy denied: ")

    async def test_verdict_category_source(self) -> None:
        # sanity: the boundary call really is a BOUNDARY verdict under
        # the shared service (the waive path keys on this).
        verdict = _service(_FixedRoot(WS)).evaluate_file_tool("write", "/etc/hosts")
        assert verdict.category is GuardCategory.BOUNDARY


# ---------------------------------------------------------------------------
# anchor derivation — the shared pure function
# ---------------------------------------------------------------------------


class TestApprovalAnchor:
    def test_relative_file_path_anchors_to_root(self) -> None:
        from modex_agent.sandbox.decision import approval_anchor

        assert (
            approval_anchor("write", {"path": "notes/a.md"}, WS)
            == str((WS / "notes" / "a.md").resolve(strict=False))
        )

    def test_absolute_path_unchanged(self) -> None:
        from modex_agent.sandbox.decision import approval_anchor

        assert (
            approval_anchor("write", {"path": "/etc/hosts"}, None)
            == str(Path("/etc/hosts").resolve(strict=False))
        )

    def test_bash_anchor_is_command(self) -> None:
        from modex_agent.sandbox.decision import approval_anchor

        assert approval_anchor("bash", {"command": "ls /tmp"}, None) == "ls /tmp"

    def test_web_anchor_is_url(self) -> None:
        from modex_agent.sandbox.decision import approval_anchor

        assert (
            approval_anchor("web_reader", {"url": "https://x.example"}, None)
            == "https://x.example"
        )

    def test_missing_arguments_are_none(self) -> None:
        from modex_agent.sandbox.decision import approval_anchor

        assert approval_anchor("write", {}, WS) is None
        assert approval_anchor("write", {"path": ""}, WS) is None
        assert approval_anchor("bash", {}, None) is None
