"""Tests for the approval REST endpoints (GET + POST /approvals).

GET reads pending approval requests directly from the pool's turn store
(mirroring ``_handle_get_todos``'s direct-file-read pattern), so a suspended
approval snapshot on disk surfaces as pending views without a live pipeline.

POST pushes an approve/deny decision through the WebUI input pipeline by
embedding an ``ApprovalDecisionInput`` in the envelope metadata, converging
on the agent pipeline's approval branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer
from modex_agent.agents.react.state import (
    ReActNode,
    ReActRuntimeStateCodec,
    ReActSnapshotPolicy,
    ReActTurnState,
)
from modex_agent.approval.constants import (
    ApprovalDecision,
    ApprovalStatus,
    ApprovalTier,
)
from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.core.session_id import SessionInfo
from modex_agent.runtime.codec import RuntimeStateCodecRegistry
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
from modex_agent.runtime.store import JsonFileTurnStateStore
from modex_agent.workspace.paths import WorkspacePaths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingInputPipeline:
    """Records the envelope handed to ``handle``; returns a neutral result."""

    def __init__(self) -> None:
        self.received: list = []  # captured UserInputEnvelope instances

    async def handle(self, envelope, ctx):  # noqa: ARG002 -- signature mirrors real pipeline
        self.received.append(envelope)

        class _Result:
            def should_continue(self) -> bool:
                return False

        return _Result()


def _build_server(tmp_path: Path, *, input_pipeline=None, input_ctx=None) -> WebUIServer:
    """Construct a WebUIServer rooted at *tmp_path* (home workspace)."""
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=tmp_path / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(".modex")
    server.set_agent_pool_map({"main": "main"})
    if input_pipeline is not None:
        server.set_input_pipeline(input_pipeline)
    if input_ctx is not None:
        server.set_input_context(input_ctx)
    return server


def _pending_snapshot(session_id: str, agent_name: str, tool_call_id: str) -> TurnSnapshot:
    """Build a SUSPENDED approval snapshot with one PENDING request."""
    identity = TurnIdentity(
        agent_id=agent_name,
        session=SessionInfo.from_str(session_id, default_agent_name="main"),
        turn_id="t1",
    )
    request = ApprovalRequestState(
        request_id="r1",
        approval_id="ap1",
        tool_call_id=tool_call_id,
        tool_name="write_file",
        arguments=ToolArguments(values={"path": "/dangerous"}),
        tier=ApprovalTier.DANGEROUS,
        iteration=1,
    )
    approval = ApprovalTransaction(
        approval_id="ap1",
        turn_id=identity.turn_id,
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch1"],
        requests=[request],
        decisions={},
        status=ApprovalStatus.PENDING,
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        current_node=ReActNode.TOOL,
        approval=approval,
    )
    return ReActSnapshotPolicy().capture(state, SnapshotReason.TOOL_APPROVAL_REQUIRED)


def _mixed_snapshot(
    session_id: str,
    agent_name: str,
    *,
    decisions: dict[str, ApprovalDecision],
) -> TurnSnapshot:
    """Build a SUSPENDED approval snapshot with three requests.

    ``decisions`` maps ``tool_call_id`` -> ``ApprovalDecision`` for whichever
    requests have already been decided; the rest remain PENDING (absent from
    the map). ``tool_call_id`` values are ``"c1"``, ``"c2"``, ``"c3"``.
    """
    identity = TurnIdentity(
        agent_id=agent_name,
        session=SessionInfo.from_str(session_id, default_agent_name="main"),
        turn_id="t1",
    )
    requests = [
        ApprovalRequestState(
            request_id=f"r{i}",
            approval_id="ap1",
            tool_call_id=call_id,
            tool_name="write_file",
            arguments=ToolArguments(values={"path": f"/dangerous/{call_id}"}),
            tier=ApprovalTier.DANGEROUS,
            iteration=1,
        )
        for i, call_id in enumerate(("c1", "c2", "c3"), start=1)
    ]
    approval = ApprovalTransaction(
        approval_id="ap1",
        turn_id=identity.turn_id,
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch1"],
        requests=requests,
        decisions=dict(decisions),
        status=ApprovalStatus.PENDING,
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        current_node=ReActNode.TOOL,
        approval=approval,
    )
    return ReActSnapshotPolicy().capture(state, SnapshotReason.TOOL_APPROVAL_REQUIRED)


def _turns_dir(workspace_root: Path) -> Path:
    """The turns dir the server resolves for the home ``main`` pool."""
    return WorkspacePaths(root=workspace_root / ".modex").runtime_dir("main", "turns")


def _make_turn_store(turns_dir: Path) -> JsonFileTurnStateStore:
    codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
    return JsonFileTurnStateStore(turns_dir, codec_registry)


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_approvals_empty_when_no_snapshot() -> None:
    """No suspended snapshot on disk -> GET returns an empty list."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server = _build_server(workspace_root)

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions/abc123.main/approvals")
            assert resp.status == 200
            assert await resp.json() == []
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_get_approvals_returns_pending_views() -> None:
    """A saved SUSPENDED approval snapshot surfaces as one pending view."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server = _build_server(workspace_root)

        session_id = "abc123.main"
        store = _make_turn_store(_turns_dir(workspace_root))
        await store.save_turn(_pending_snapshot(session_id, agent_name="main", tool_call_id="c1"))

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/approvals")
            assert resp.status == 200
            data = await resp.json()
            assert len(data) == 1
            view = data[0]
            assert view["tool_call_id"] == "c1"
            assert view["tool_name"] == "write_file"
            assert view["tier"] == str(ApprovalTier.DANGEROUS)
            assert view["status"] == "pending"
            assert view["arguments"] == {"path": "/dangerous"}
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_get_approvals_excludes_decided_requests() -> None:
    """GET returns only PENDING requests; allowed/denied/preempted are absent."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server = _build_server(workspace_root)

        session_id = "abc123.main"
        store = _make_turn_store(_turns_dir(workspace_root))
        # c1 allowed, c2 denied, c3 still pending.
        await store.save_turn(
            _mixed_snapshot(
                session_id,
                agent_name="main",
                decisions={
                    "c1": ApprovalDecision.ALLOWED,
                    "c2": ApprovalDecision.DENIED,
                },
            )
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/approvals")
            assert resp.status == 200
            data = await resp.json()
            # Only the genuinely-pending request (c3) comes back.
            assert {view["tool_call_id"] for view in data} == {"c3"}
            assert len(data) == 1
            assert data[0]["status"] == "pending"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_get_approvals_empty_when_all_decided() -> None:
    """When every request is already decided, GET returns an empty list."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server = _build_server(workspace_root)

        session_id = "abc123.main"
        store = _make_turn_store(_turns_dir(workspace_root))
        await store.save_turn(
            _mixed_snapshot(
                session_id,
                agent_name="main",
                decisions={
                    "c1": ApprovalDecision.ALLOWED,
                    "c2": ApprovalDecision.DENIED,
                    "c3": ApprovalDecision.PREEMPTED,
                },
            )
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/approvals")
            assert resp.status == 200
            assert await resp.json() == []
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_get_approvals_resolves_snapshot_without_agent_id_scope() -> None:
    """The agent_id-free scope still resolves the snapshot (regression guard).

    The handler drops ``agent_id`` from ``StateQueryScope`` to match
    ``ApprovalResumer.load_pending``. Approval turns are partitioned by
    workspace + pool + session_id, so the snapshot must still be found.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        server = _build_server(workspace_root)

        session_id = "abc123.main"
        store = _make_turn_store(_turns_dir(workspace_root))
        await store.save_turn(_pending_snapshot(session_id, agent_name="main", tool_call_id="c1"))

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/approvals")
            assert resp.status == 200
            data = await resp.json()
            assert len(data) == 1
            assert data[0]["tool_call_id"] == "c1"
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_approval_runs_input_pipeline_with_decision() -> None:
    """POST ``allow`` embeds an ApprovalDecisionInput and runs the pipeline."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        pipeline = _RecordingInputPipeline()
        server = _build_server(workspace_root, input_pipeline=pipeline, input_ctx=object())

        session_id = "abc123.main"
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.post(
                f"/api/sessions/{session_id}/approvals",
                json={"tool_call_id": "c1", "action": "allow"},
            )
            assert resp.status == 202
            body = await resp.json()
            assert body == {"accepted": True}
        finally:
            await client.close()

        assert len(pipeline.received) == 1
        envelope = pipeline.received[0]
        decision = envelope.metadata[RoutingMeta.APPROVAL_DECISION]
        assert decision == ApprovalDecisionInput("c1", ApprovalAction.ALLOW)


@pytest.mark.asyncio
async def test_post_approval_stamps_workspace_from_ws_query() -> None:
    """POST stamps the workspace the snapshot lives under — otherwise
    ``ResolveWorkspaceStage`` falls back to home, the dispatcher binds the wrong
    workspace, and ``load_pending`` finds nothing (the "approve does nothing"
    bug)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp) / "ws_under_test"
        workspace_root.mkdir()
        pipeline = _RecordingInputPipeline()
        server = _build_server(Path(tmp), input_pipeline=pipeline, input_ctx=object())

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.post(
                f"/api/sessions/abc123.main/approvals?ws={workspace_root}",
                json={"tool_call_id": "c1", "action": "allow"},
            )
            assert resp.status == 202
        finally:
            await client.close()

        envelope = pipeline.received[0]
        assert envelope.metadata[RoutingMeta.WORKSPACE] == str(workspace_root.resolve())


@pytest.mark.asyncio
async def test_post_approval_rejects_invalid_action() -> None:
    """An unknown action value yields 400, not a server error."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        pipeline = _RecordingInputPipeline()
        server = _build_server(workspace_root, input_pipeline=pipeline, input_ctx=object())

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/sessions/abc123.main/approvals",
                json={"tool_call_id": "c1", "action": "bogus"},
            )
            assert resp.status == 400
        finally:
            await client.close()
        assert pipeline.received == []


@pytest.mark.asyncio
async def test_post_approval_rejects_missing_tool_call_id() -> None:
    """Missing tool_call_id yields 400."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        pipeline = _RecordingInputPipeline()
        server = _build_server(workspace_root, input_pipeline=pipeline, input_ctx=object())

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/sessions/abc123.main/approvals",
                json={"action": "allow"},
            )
            assert resp.status == 400
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_post_approval_503_when_pipeline_not_configured() -> None:
    """No input pipeline injected -> 503, not a crash."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        # No input_pipeline / input_ctx injected.
        server = _build_server(workspace_root)

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/sessions/abc123.main/approvals",
                json={"tool_call_id": "c1", "action": "allow"},
            )
            assert resp.status == 503
        finally:
            await client.close()
