"""ApprovalResumer — approval state machine (decision/save/prompt/restore).

Exercises the resumer directly, asserting the load-bearing approval-resume
semantics are preserved after extraction from the pipeline:

* some requests still PENDING → save_turn called, prompt rendered, returns None
* all decided (or action decides the last pending) → state restored into
  agent_context.runtime.state, returns the resolved TurnStateStore
* approval_from_snapshot returns None → return None immediately
* no turn_store resolvable + not all decided → log error, return None (no crash)
* all decided but agent_context.runtime is None → return None (cannot restore)

On the success path the returned store is the SAME store object the resumer
resolved (pool_data.turn_store when present, else the resumer's own turn_store)
— so the caller can ``delete_turn`` + ``drain`` without re-resolving it.

The driving tail (execute_turn / delete_turn / drain) is owned by the caller
and is therefore absent here — those are covered by the pipeline-level approval
regression suite.
"""
from __future__ import annotations

import pytest

from modex_agent.agents.react.state import (
    ReActNode,
    ReActSnapshotPolicy,
    ReActTurnState,
)
from modex_agent.approval.constants import (
    ApprovalDecision,
    ApprovalStatus,
    ApprovalTier,
)
from modex_agent.approval.types import ApprovalAction
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.memory.history import ListMessageHistory
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.runtime.enums import (
    AgentKind,
    ApprovalSubjectType,
    SnapshotReason,
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
from modex_agent.runtime.store import InMemoryTurnStateStore, TurnStateStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingUI:
    """Records render_message / render_approval_prompt calls (session_id, content)."""

    def __init__(self) -> None:
        self.rendered: list[tuple[str, str]] = []

    async def render_message(self, session_id, content, metadata=None) -> str:
        self.rendered.append((session_id, content))
        return "msg-id"

    async def render_approval_prompt(self, session_id, view) -> None:
        # Record the same way render_message does so existing assertions
        # (``assert user_interface.rendered``) stay valid.
        self.rendered.append((session_id, view.tool_name))


class _RecordingTurnStore(TurnStateStore):
    """Records save_turn/delete_turn/list_active_turns; delegates to in-mem."""

    def __init__(self) -> None:
        self._inner = InMemoryTurnStateStore()
        self.saved: list[TurnSnapshot] = []
        self.deleted: list[TurnIdentity] = []

    async def save_turn(self, snapshot: TurnSnapshot) -> None:
        self.saved.append(snapshot)
        await self._inner.save_turn(snapshot)

    async def load_turn(self, identity):
        return await self._inner.load_turn(identity)

    async def delete_turn(self, identity: TurnIdentity) -> None:
        self.deleted.append(identity)
        await self._inner.delete_turn(identity)

    async def list_active_turns(self, scope):
        return await self._inner.list_active_turns(scope)


class _FakeAgent:
    """Minimal agent stand-in exposing the .name attribute the resumer needs."""

    def __init__(self, name: str = "agent") -> None:
        self.name = name


# ---------------------------------------------------------------------------
# Snapshot / context builders
# ---------------------------------------------------------------------------


def _request(rid: str, call_id: str, *, approval_id: str = "ap1") -> ApprovalRequestState:
    return ApprovalRequestState(
        request_id=rid,
        approval_id=approval_id,
        tool_call_id=call_id,
        tool_name="write_file",
        arguments=ToolArguments(values={"path": "/dangerous"}),
        tier=ApprovalTier.DANGEROUS,
        iteration=1,
    )


def _snapshot(
    decisions: dict[str, ApprovalDecision] | None = None,
    *,
    approval_requests: list[ApprovalRequestState] | None = None,
    session_id: str = "s1",
    include_approval: bool = True,
) -> TurnSnapshot:
    identity = TurnIdentity(
        agent_id="agent",
        session=SessionInfo.from_str(session_id, default_agent_name="main"),
        turn_id="t1",
    )
    requests = approval_requests if approval_requests is not None else [
        _request("r1", "c1"),
        _request("r2", "c2"),
    ]
    approval = ApprovalTransaction(
        approval_id="ap1",
        turn_id=identity.turn_id,
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch1"],
        requests=requests,
        decisions=decisions or {},
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        current_node=ReActNode.TOOL,
        approval=approval if include_approval else None,
    )
    snapshot = ReActSnapshotPolicy().capture(
        state, SnapshotReason.TOOL_APPROVAL_REQUIRED,
    )
    if not include_approval:
        # Strip the serialized approval payload so approval_from_snapshot -> None.
        payload = dict(snapshot.state_payload)
        payload.pop("approval", None)
        snapshot.state_payload = payload
    return snapshot


def _make_agent_context(*, with_runtime: bool = True) -> AgentContext:
    manager = InMemoryToolManager()
    ctx = AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=manager,
        session=SessionInfo.from_str("s1.main"),
        max_iterations=5,
    )
    if with_runtime:
        identity = TurnIdentity(
            agent_id="agent",
            session=SessionInfo.from_str("s1.main"),
            turn_id="t1",
        )
        ctx.runtime = AgentRuntime(
            services=AgentRuntimeServices(turn_store=None),
            state=ReActTurnState(
                identity=identity,
                agent_kind=AgentKind.REACT,
                phase=TurnPhase.CREATED,
            ),
        )
    return ctx


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def turn_store() -> _RecordingTurnStore:
    return _RecordingTurnStore()


@pytest.fixture
def user_interface() -> _RecordingUI:
    return _RecordingUI()


@pytest.fixture
def resumer(turn_store, user_interface) -> ApprovalResumer:
    return ApprovalResumer(
        agent=_FakeAgent(),
        turn_store=turn_store,
        user_interface=user_interface,
    )


@pytest.fixture
def partial_snapshot() -> TurnSnapshot:
    """Two requests, neither decided → not every_tool_decided."""
    return _snapshot(decisions={})


@pytest.fixture
def complete_snapshot() -> TurnSnapshot:
    """Two requests, both ALLOWED → every_tool_decided."""
    return _snapshot(
        decisions={
            "c1": ApprovalDecision.ALLOWED,
            "c2": ApprovalDecision.ALLOWED,
        },
    )


@pytest.fixture
def snapshot_no_approval() -> TurnSnapshot:
    """Snapshot with no approval payload → approval_from_snapshot returns None."""
    return _snapshot(include_approval=False)


@pytest.fixture
def snapshot_no_store(resumer) -> tuple[ApprovalResumer, TurnSnapshot]:
    """A resumer whose turn_store is None, paired with a partial snapshot."""
    no_store_resumer = ApprovalResumer(
        agent=_FakeAgent(),
        turn_store=None,
        user_interface=None,
    )
    return no_store_resumer, _snapshot(decisions={})


@pytest.fixture
def fake_agent_context() -> AgentContext:
    return _make_agent_context(with_runtime=True)


@pytest.fixture
def fake_agent_context_no_runtime() -> AgentContext:
    return _make_agent_context(with_runtime=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_apply_resume_partial_saves_and_returns_none(
    resumer, partial_snapshot, turn_store, user_interface,
):
    """Some requests still PENDING → save_turn called, prompt rendered, returns None."""
    result = await resumer.apply_resume(
        partial_snapshot, action=None, session_id="s1", pool_data=None,
        agent_context=fake_agent_context_for(partial_snapshot),
    )
    assert result is None
    assert turn_store.saved  # save_turn invoked with updated snapshot
    assert user_interface.rendered  # prompt rendered for the first PENDING req


async def test_apply_resume_complete_restores_state_and_returns_store(
    resumer, complete_snapshot, fake_agent_context,
):
    """All decided → state restored into agent_context, returns the resolved store."""
    result = await resumer.apply_resume(
        complete_snapshot, action=None, session_id="s1", pool_data=None,
        agent_context=fake_agent_context,
    )
    assert result is resumer._turn_store  # the store the caller should clean up with
    assert fake_agent_context.runtime is not None  # restored from snapshot
    assert fake_agent_context.identity == complete_snapshot.identity


async def test_apply_resume_action_decides_last_pending_returns_store(
    resumer, partial_snapshot, fake_agent_context,
):
    """action=ALLOW decides the first PENDING req → completes the set → returns store."""
    # Seed both as PENDING; applying ALLOW to the first makes it the only
    # decision, but the second is still PENDING → still None.
    result_partial = await resumer.apply_resume(
        partial_snapshot, action=ApprovalAction.ALLOW, session_id="s1",
        pool_data=None, agent_context=fake_agent_context,
    )
    assert result_partial is None  # one decided, one still pending

    # Now build a single-request snapshot: deciding it completes the set.
    single = _snapshot(
        decisions={},
        approval_requests=[_request("r1", "c1")],
    )
    result = await resumer.apply_resume(
        single, action=ApprovalAction.ALLOW, session_id="s1",
        pool_data=None, agent_context=fake_agent_context,
    )
    assert result is resumer._turn_store


async def test_apply_resume_no_turn_store_returns_none(snapshot_no_store):
    """No turn_store resolvable + not all decided → logs error, returns None."""
    no_store_resumer, snapshot = snapshot_no_store
    result = await no_store_resumer.apply_resume(
        snapshot, action=None, session_id="s1", pool_data=None,
        agent_context=fake_agent_context_for(snapshot),
    )
    assert result is None


async def test_apply_resume_approval_none_returns_none(
    resumer, snapshot_no_approval, fake_agent_context,
):
    """approval_from_snapshot returns None → return None immediately."""
    result = await resumer.apply_resume(
        snapshot_no_approval, action=ApprovalAction.ALLOW, session_id="s1",
        pool_data=None, agent_context=fake_agent_context,
    )
    assert result is None


async def test_apply_resume_complete_but_runtime_none_returns_none(
    resumer, complete_snapshot, fake_agent_context_no_runtime,
):
    """All decided but agent_context.runtime is None → return None (cannot restore)."""
    result = await resumer.apply_resume(
        complete_snapshot, action=ApprovalAction.ALLOW, session_id="s1",
        pool_data=None, agent_context=fake_agent_context_no_runtime,
    )
    assert result is None


async def test_apply_resume_pool_data_turn_store_used(
    resumer, partial_snapshot, turn_store, user_interface,
):
    """When pool_data carries its own turn_store, that store is used for save_turn."""
    pool_store = _RecordingTurnStore()
    pool_data = _PoolDataSnapshotStub(turn_store=pool_store)
    result = await resumer.apply_resume(
        partial_snapshot, action=None, session_id="s1", pool_data=pool_data,
        agent_context=fake_agent_context_for(partial_snapshot),
    )
    assert result is None  # partial → no resume
    assert pool_store.saved  # pool_data.turn_store, not the resumer's own
    assert not turn_store.saved


async def test_load_pending_returns_none_when_no_store(resumer):
    """No turn_store on the resumer → load_pending returns None."""
    assert await resumer.load_pending("s1", pool_data=None) is None


async def test_load_pending_returns_last_by_created_at(
    turn_store, resumer,
):
    """load_pending returns the most-recently-created suspended approval snapshot."""
    older = _snapshot()
    newer = _snapshot()
    # Force distinct created_at so ordering is deterministic.
    older.created_at = 1000.0
    newer.created_at = 2000.0
    await turn_store.save_turn(older)
    await turn_store.save_turn(newer)
    pending = await resumer.load_pending("s1")
    assert pending is not None
    assert pending.identity == newer.identity


async def test_load_pending_returns_none_when_no_snapshots(turn_store, resumer):
    pending = await resumer.load_pending("s1")
    assert pending is None


async def test_apply_resume_targets_specific_call_id(
    resumer, partial_snapshot, turn_store, fake_agent_context,
):
    """tool_call_id given → only that request decided; the other stays PENDING."""
    result = await resumer.apply_resume(
        partial_snapshot, action=ApprovalAction.ALLOW, session_id="s1",
        pool_data=None, agent_context=fake_agent_context, tool_call_id="c2",
    )
    assert result is None  # c1 still pending → not every tool decided
    saved = turn_store.saved[-1]
    approval = ReActSnapshotPolicy.approval_from_snapshot(saved)
    assert approval.decisions["c2"] == ApprovalDecision.ALLOWED
    assert approval.decisions.get("c1", ApprovalDecision.PENDING) == ApprovalDecision.PENDING


async def test_apply_resume_targeted_call_id_completes_when_last_pending(
    resumer, fake_agent_context, turn_store,
):
    """Targeting the only remaining PENDING request completes the set → returns store."""
    snapshot = _snapshot(decisions={"c1": ApprovalDecision.ALLOWED})  # c2 still PENDING
    result = await resumer.apply_resume(
        snapshot, action=ApprovalAction.ALLOW, session_id="s1",
        pool_data=None, agent_context=fake_agent_context, tool_call_id="c2",
    )
    assert result is resumer._turn_store


async def test_apply_resume_targeting_already_decided_is_noop(
    resumer, turn_store, user_interface, fake_agent_context,
):
    """Targeting an already-decided (or unknown) call_id decides nothing:
    the other PENDING request stays pending → partial save + prompt, None.

    Guards the webui double-submit / stale-card path: re-sending a decision
    for a request that was already resolved must not flip any state.
    """
    # c1 already ALLOWED, c2 still PENDING; target c1 again (already decided).
    snapshot = _snapshot(decisions={"c1": ApprovalDecision.ALLOWED})
    result = await resumer.apply_resume(
        snapshot, action=ApprovalAction.ALLOW, session_id="s1",
        pool_data=None, agent_context=fake_agent_context, tool_call_id="c1",
    )
    assert result is None  # c2 still pending
    saved = turn_store.saved[-1]
    approval = ReActSnapshotPolicy.approval_from_snapshot(saved)
    assert approval.decisions["c1"] == ApprovalDecision.ALLOWED  # unchanged
    assert approval.decisions.get("c2", ApprovalDecision.PENDING) == ApprovalDecision.PENDING
    assert user_interface.rendered  # c2 prompt rendered (the remaining pending)


# ---------------------------------------------------------------------------
# Deny-seals-the-batch regression guards (ADR-0011)
#
# ApprovalTransaction.apply_decision preempts every other PENDING/ALLOWED
# request when a DENIED decision lands, sets status=DENIED, and thereby flips
# every_tool_decided to True — so the turn resumes. This is the intended
# behaviour for BOTH channels (webui targeted deny AND IM next-pending deny).
# These tests lock the invariant so a future refactor of apply_resume (e.g. a
# misguided "short-circuit deny" patch) or of apply_decision cannot silently
# break it. See ADR-0011 decision 2 (KEEP deny-seals-batch).
# ---------------------------------------------------------------------------


async def test_webui_deny_seals_batch_all_pending(
    resumer, fake_agent_context,
):
    """webui DENY on one request preempts every other PENDING request → resume.

    3 PENDING requests; deny req1 via tool_call_id. req1 → DENIED, req2/req3 →
    PREEMPTED, every_tool_decided True, apply_resume returns the turn_store
    (resume path, not the partial-save path).
    """
    snapshot = _snapshot(
        decisions={},
        approval_requests=[
            _request("r1", "c1"),
            _request("r2", "c2"),
            _request("r3", "c3"),
        ],
    )
    result = await resumer.apply_resume(
        snapshot, action=ApprovalAction.DENY, session_id="s1",
        pool_data=None, agent_context=fake_agent_context, tool_call_id="c1",
    )
    assert result is resumer._turn_store  # resumed, not partial-saved
    approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
    assert approval.decisions["c1"] == ApprovalDecision.DENIED
    assert approval.decisions["c2"] == ApprovalDecision.PREEMPTED
    assert approval.decisions["c3"] == ApprovalDecision.PREEMPTED
    assert approval.every_tool_decided is True
    assert approval.status == ApprovalStatus.DENIED


async def test_webui_deny_preempts_already_allowed(
    resumer, fake_agent_context,
):
    """webui DENY preempts a previously-ALLOWED request too → resume.

    req1 ALLOWED, req2/req3 PENDING; deny req2. req2 → DENIED, req1 (allowed) →
    PREEMPTED, req3 → PREEMPTED, every_tool_decided True, returns turn_store.
    Guards that ALLOWED is not exempt from deny-preempt.
    """
    snapshot = _snapshot(
        decisions={"c1": ApprovalDecision.ALLOWED},
        approval_requests=[
            _request("r1", "c1"),
            _request("r2", "c2"),
            _request("r3", "c3"),
        ],
    )
    result = await resumer.apply_resume(
        snapshot, action=ApprovalAction.DENY, session_id="s1",
        pool_data=None, agent_context=fake_agent_context, tool_call_id="c2",
    )
    assert result is resumer._turn_store
    approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
    assert approval.decisions["c2"] == ApprovalDecision.DENIED
    assert approval.decisions["c1"] == ApprovalDecision.PREEMPTED  # was ALLOWED
    assert approval.decisions["c3"] == ApprovalDecision.PREEMPTED
    assert approval.every_tool_decided is True
    assert approval.status == ApprovalStatus.DENIED


async def test_webui_approve_is_per_request(
    resumer, fake_agent_context, turn_store,
):
    """webui ALLOW is per-request: only the target flips, others stay PENDING.

    Regression guard mirroring the deny-seals tests: ALLOW must NOT seal the
    batch. 3 PENDING; allow req1 → req1 ALLOWED, req2/req3 still PENDING,
    every_tool_decided False, partial-save path → returns None.
    """
    snapshot = _snapshot(
        decisions={},
        approval_requests=[
            _request("r1", "c1"),
            _request("r2", "c2"),
            _request("r3", "c3"),
        ],
    )
    result = await resumer.apply_resume(
        snapshot, action=ApprovalAction.ALLOW, session_id="s1",
        pool_data=None, agent_context=fake_agent_context, tool_call_id="c1",
    )
    assert result is None  # partial path
    saved = turn_store.saved[-1]
    approval = ReActSnapshotPolicy.approval_from_snapshot(saved)
    assert approval.decisions["c1"] == ApprovalDecision.ALLOWED
    assert approval.decisions.get("c2", ApprovalDecision.PENDING) == ApprovalDecision.PENDING
    assert approval.decisions.get("c3", ApprovalDecision.PENDING) == ApprovalDecision.PENDING
    assert approval.every_tool_decided is False


async def test_im_deny_seals_batch_and_im_allow_is_partial(
    resumer, fake_agent_context, turn_store,
):
    """IM path (tool_call_id=None) current behaviour — regression guard.

    A) DENY with no tool_call_id decides the next-PENDING request, and because
       apply_decision seals on DENY, the remaining requests are PREEMPTED and
       the turn resumes (returns turn_store). This documents that IM deny also
       seals — the intended ADR-0011 behaviour, NOT one-at-a-time.
    B) ALLOW with no tool_call_id decides only the next-PENDING request; the
       others stay PENDING → partial path (returns None).
    """
    # --- A: IM deny seals the batch ---
    snapshot = _snapshot(
        decisions={},
        approval_requests=[
            _request("r1", "c1"),
            _request("r2", "c2"),
            _request("r3", "c3"),
        ],
    )
    result_deny = await resumer.apply_resume(
        snapshot, action=ApprovalAction.DENY, session_id="s1",
        pool_data=None, agent_context=fake_agent_context, tool_call_id=None,
    )
    assert result_deny is resumer._turn_store  # sealed → resume
    approval_deny = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
    # The first PENDING request (c1) is the one apply_resume decided...
    assert approval_deny.decisions["c1"] == ApprovalDecision.DENIED
    # ...and apply_decision preempted the rest.
    assert approval_deny.decisions["c2"] == ApprovalDecision.PREEMPTED
    assert approval_deny.decisions["c3"] == ApprovalDecision.PREEMPTED
    assert approval_deny.every_tool_decided is True
    assert approval_deny.status == ApprovalStatus.DENIED

    # --- B: IM allow is per-request (partial) ---
    snapshot_allow = _snapshot(
        decisions={},
        approval_requests=[
            _request("r1", "c1"),
            _request("r2", "c2"),
            _request("r3", "c3"),
        ],
    )
    result_allow = await resumer.apply_resume(
        snapshot_allow, action=ApprovalAction.ALLOW, session_id="s1",
        pool_data=None, agent_context=fake_agent_context, tool_call_id=None,
    )
    assert result_allow is None  # partial path
    saved = turn_store.saved[-1]
    approval_allow = ReActSnapshotPolicy.approval_from_snapshot(saved)
    assert approval_allow.decisions["c1"] == ApprovalDecision.ALLOWED
    assert approval_allow.decisions.get("c2", ApprovalDecision.PENDING) == ApprovalDecision.PENDING
    assert approval_allow.decisions.get("c3", ApprovalDecision.PENDING) == ApprovalDecision.PENDING
    assert approval_allow.every_tool_decided is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fake_agent_context_for(snapshot: TurnSnapshot) -> AgentContext:
    """A fresh agent_context carrying a runtime (used where a fixture won't do)."""
    return _make_agent_context(with_runtime=True)


class _PoolDataSnapshotStub:
    """Minimal stand-in exposing only the .turn_store field the resumer reads."""

    def __init__(self, turn_store: TurnStateStore) -> None:
        self.turn_store = turn_store
