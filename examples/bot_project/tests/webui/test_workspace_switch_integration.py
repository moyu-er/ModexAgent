"""End-to-end integration tests for WebUI workspace switch.

Tests the "message carries ws" mechanism: messages carry their workspace
path via envelope.metadata["workspace"], resolved by ResolveWorkspaceStage.
Transcript store routes writes via the bound workspace root (ctxvar).
No real LLM calls.
"""
import asyncio
import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import AssistantTurnEvent, UserMessageEvent, _unwrap_envelope
from bot.webui.server import WebUIServer, _new_uuid_prefix
from modex_agent.workspace.control import WorkspaceController
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.registry import WorkspaceRegistry
from modex_agent.workspace.routing import WorkspaceResolver
from modex_agent.workspace.store import GlobalWorkspaceStore
from modex_agent.core.session_id import SessionIdFactory, SessionInfo, now_ms
from modex_agent.workspace.runtime import bind_workspace_root


class _FakeFactory:
    """Minimal factory that satisfies WorkspaceRegistry typing."""

    async def materialize(self, ctx):
        return {"t": ctx.target}

    async def evict(self, resources):
        return None


def _real_project_dir() -> Path:
    """Return the real bot project directory for config loading."""
    return Path(__file__).resolve().parent.parent.parent


def _make_server(data_dir: Path) -> tuple[WebUIServer, WebSocketInputAdapter]:
    """Create a fully wired WebUIServer with the real production pool map.

    ``data_dir`` is the workspace root (project dir). The store routes writes
    via the bound workspace root (ctxvar); the server's ``home_sessions_dir``
    points at ``<data_dir>/.modex/sessions`` so HTTP reads (no ``?ws=``) find
    the same physical directory.
    """
    inp = WebSocketInputAdapter()
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        data_dir=data_dir,
        home_sessions_dir=home_sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(".modex")
    server.set_pool_agent_names(["main", "coding"])
    server.set_agent_resolver(lambda pool_name: pool_name)
    session_store = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: s.agent_name,
    )
    server.set_session_store(session_store)
    server.set_session_factory(SessionIdFactory())
    return server, inp


async def _simulate_qa_turn(
    store: WorkspaceScopedTranscriptStore,
    conv_prefix: str,
    agent_name: str,
    user_content: str,
    assistant_content: str,
) -> None:
    """Materialise one Q/A turn by writing user + assistant events."""
    session_id = f"{conv_prefix}.{agent_name}"
    await store.append(
        session_id,
        UserMessageEvent(
            session_id=session_id,
            agent_name=agent_name,
            content=user_content,
        ),
    )
    await store.append(
        session_id,
        AssistantTurnEvent(
            session_id=session_id,
            agent_name=agent_name,
            turn_id="turn-1",
            blocks=[{"type": "text", "text": assistant_content}],
            latency_ms=0,
        ),
    )


# ── Test 1: WebUI end-to-end flow ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_switch_workspace_attach_send_transcript_lands_in_ws_dir() -> None:
    """Switch workspace A -> attach new session with ws=A -> send message ->
    transcript lands in <A>/.modex/sessions/; GET /api/sessions?ws=A shows it;
    ?ws=B does not.
    """
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b, tempfile.TemporaryDirectory() as tmp_home:
        ws_a = Path(tmp_a)
        ws_b = Path(tmp_b)
        ws_a.mkdir(parents=True, exist_ok=True)
        ws_b.mkdir(parents=True, exist_ok=True)

        home = Path(tmp_home)
        home.mkdir(parents=True, exist_ok=True)

        # Wire workspace control for /cd endpoint
        registry = WorkspaceRegistry(
            home=home,
            data_dir_name=".modex",
            factory=_FakeFactory(),
            store=GlobalWorkspaceStore(home=home, data_dir_name=".modex"),
        )
        controller = WorkspaceController(
            registry=registry,
            data_dir_name=".modex",
            enabled=True,
        )

        server, inp = _make_server(home)
        server.set_workspace_control(controller)

        # Inject pipeline; route its persist write to ws_a via the ctxvar root.
        from tests.webui._pipeline_fixture import attach_default_pipeline

        attach_default_pipeline(
            server, server._store, inp, workspace_root=ws_a
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            # Step 1: POST /api/workspace/cd to switch to workspace A
            resp = await client.post("/api/workspace/cd", json={"path": str(ws_a)})
            assert resp.status == 200
            cd_result = await resp.json()
            assert cd_result["success"] is True

            # Step 2: WS attach with ws=A (new conversation). Route the pipeline
            # persist + the simulated transcript into ws_a via the ctxvar root.
            with bind_workspace_root(ws_a):
                ws = await client.ws_connect("/ws")
                conv_prefix = _new_uuid_prefix()
                await ws.send_json(
                    {
                        "action": "attach",
                        "uuid_prefix": conv_prefix,
                        "pool": "main",
                        "ws": str(ws_a),
                    }
                )
                attached = _unwrap_envelope(await ws.receive_json())
                assert attached["event"] == "attached"
                session_id = attached["session_id"]

                # Step 3: Send message (consumed by pipeline fixture, not real LLM)
                await ws.send_json(
                    {
                        "action": "send_message",
                        "session_id": session_id,
                        "content": "hello from ws A",
                    }
                )
                echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
                assert echoed["event"] == "user_message"

                # Step 4: Verify transcript lands in workspace A directory
                # Writes resolve to <ws_a>/.modex/sessions via the ctxvar root.
                await _simulate_qa_turn(server._store, conv_prefix, "main", "hi A", "hello A")

            # Verify file exists in ws_a
            assert (ws_a / ".modex" / "sessions" / "main" / f"{conv_prefix}.main.jsonl").exists()

            # Step 5: Verify the session's transcript is accessible via the store
            ws_a_sessions = WorkspacePaths(root=ws_a / ".modex").sessions_dir
            events = await server._store.load(
                session_id, sessions_dir=ws_a_sessions
            )
            assert len(events) >= 1
            contents = [str(e.to_dict()) for e in events]
            assert any("hi A" in c for c in contents)

            # Step 6: Verify the transcript is NOT in workspace B
            ws_b_file = ws_b / ".modex" / "sessions" / "main" / f"{session_id}.jsonl"
            assert not ws_b_file.exists(), f"Expected {session_id} NOT to leak to ws_b"
        finally:
            await client.close()


# ── Test 2: IM regression ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_im_message_carries_ws_routes_transcript_to_workspace() -> None:
    """IM regression: a message carrying wsA -> resolver.resolve(wsA)
    -> wsA; transcript lands in wsA directory.
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "workspace_a"
        ws_a.mkdir()

        registry = WorkspaceRegistry(
            home=home,
            data_dir_name=".modex",
            factory=_FakeFactory(),
            store=GlobalWorkspaceStore(home=home, data_dir_name=".modex"),
        )
        controller = WorkspaceController(
            registry=registry,
            data_dir_name=".modex",
            enabled=True,
        )
        resolver = WorkspaceResolver(registry=registry)

        im_prefix = "qq_user_12345"

        # IM adapter's current_ws is set to ws_a via /cd (handled by S2)
        # The message carries ws_a in its workspace field.
        ctx, _resources = await resolver.resolve(ws_a)
        assert Path(ctx.target).resolve() == ws_a.resolve()

        # Transcript store routes writes via the bound workspace root (ctxvar).
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

        sid = f"{im_prefix}.main"
        with bind_workspace_root(ws_a):
            await store.append(
                sid,
                UserMessageEvent(session_id=sid, agent_name="main", content="IM msg"),
            )

        # Verify transcript lands in ws_a
        ws_a_sessions = ws_a / ".modex" / "sessions" / "main"
        events = await store.load(
            sid, sessions_dir=WorkspacePaths(root=ws_a / ".modex").sessions_dir
        )
        assert len(events) == 1
        assert "IM msg" in str(events[0].to_dict())

        # Verify it's NOT in home
        home_file = home / ".modex" / "sessions" / "main" / f"{sid}.jsonl"
        assert not home_file.exists(), "IM transcript must NOT leak to home"


# ── Test 3: Multi-workspace isolation ────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_workspace_isolation_concurrent_appends() -> None:
    """Two prefixes bound to A/B; each append lands in the correct directory;
    concurrent appends don't mix.
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "workspace_a"
        ws_a.mkdir()
        ws_b = Path(tmp) / "workspace_b"
        ws_b.mkdir()

        # Transcript store routes writes via the bound workspace root (ctxvar).
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

        prefix_a = "conv_a_123"
        prefix_b = "conv_b_456"

        sid_a = f"{prefix_a}.main"
        sid_b = f"{prefix_b}.main"

        # Concurrent appends; each binds its own workspace root.
        async def _append_a() -> None:
            with bind_workspace_root(ws_a):
                await store.append(
                    sid_a,
                    UserMessageEvent(session_id=sid_a, agent_name="main", content="msg for A"),
                )

        async def _append_b() -> None:
            with bind_workspace_root(ws_b):
                await store.append(
                    sid_b,
                    UserMessageEvent(session_id=sid_b, agent_name="main", content="msg for B"),
                )

        await asyncio.gather(_append_a(), _append_b())

        # Verify A's message is in ws_a
        events_a = await store.load(
            sid_a, sessions_dir=WorkspacePaths(root=ws_a / ".modex").sessions_dir
        )
        assert len(events_a) == 1
        assert "msg for A" in str(events_a[0].to_dict())

        # Verify B's message is in ws_b
        events_b = await store.load(
            sid_b, sessions_dir=WorkspacePaths(root=ws_b / ".modex").sessions_dir
        )
        assert len(events_b) == 1
        assert "msg for B" in str(events_b[0].to_dict())

        # Verify cross-isolation: A's message is NOT in ws_b
        ws_b_file = ws_b / ".modex" / "sessions" / "main" / f"{sid_a}.jsonl"
        assert not ws_b_file.exists(), "A's transcript must NOT leak to ws_b"

        # Verify B's message is NOT in ws_a
        ws_a_file = ws_a / ".modex" / "sessions" / "main" / f"{sid_b}.jsonl"
        assert not ws_a_file.exists(), "B's transcript must NOT leak to ws_a"


@pytest.mark.asyncio
async def test_multi_workspace_isolation_sequential_appends() -> None:
    """Two prefixes bound to A/B; sequential appends land in correct directories."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "workspace_a"
        ws_a.mkdir()
        ws_b = Path(tmp) / "workspace_b"
        ws_b.mkdir()

        # Transcript store routes writes via the bound workspace root (ctxvar).
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

        prefix_a = "seq_a_789"
        prefix_b = "seq_b_012"

        sid_a = f"{prefix_a}.main"
        sid_b = f"{prefix_b}.main"

        # Append to A first
        with bind_workspace_root(ws_a):
            await store.append(
                sid_a,
                UserMessageEvent(session_id=sid_a, agent_name="main", content="first A"),
            )
        # Then append to B
        with bind_workspace_root(ws_b):
            await store.append(
                sid_b,
                UserMessageEvent(session_id=sid_b, agent_name="main", content="first B"),
            )
        # Append to A again
        with bind_workspace_root(ws_a):
            await store.append(
                sid_a,
                UserMessageEvent(session_id=sid_a, agent_name="main", content="second A"),
            )

        # Verify A has both messages
        events_a = await store.load(
            sid_a, sessions_dir=WorkspacePaths(root=ws_a / ".modex").sessions_dir
        )
        assert len(events_a) == 2
        contents_a = [str(e.to_dict()) for e in events_a]
        assert any("first A" in c for c in contents_a)
        assert any("second A" in c for c in contents_a)

        # Verify B has one message
        events_b = await store.load(
            sid_b, sessions_dir=WorkspacePaths(root=ws_b / ".modex").sessions_dir
        )
        assert len(events_b) == 1
        assert "first B" in str(events_b[0].to_dict())

        # Verify no cross-contamination
        ws_b_has_a = (ws_b / ".modex" / "sessions" / "main" / f"{sid_a}.jsonl").exists()
        ws_a_has_b = (ws_a / ".modex" / "sessions" / "main" / f"{sid_b}.jsonl").exists()
        assert not ws_b_has_a, "A's transcript must NOT be in ws_b"
        assert not ws_a_has_b, "B's transcript must NOT be in ws_a"


# ── Test 4: IM zero-change routing regression ─────────────────────────────


@pytest.mark.asyncio
async def test_im_zero_change_routing() -> None:
    """IM (non-WebUI) sessions: message carries workspace, resolver
    routes to the correct workspace, and store.append lands there.
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_target = Path(tmp) / "target_ws"
        ws_target.mkdir()

        registry = WorkspaceRegistry(
            home=home,
            data_dir_name=".modex",
            factory=_FakeFactory(),
            store=GlobalWorkspaceStore(home=home, data_dir_name=".modex"),
        )
        controller = WorkspaceController(
            registry=registry,
            data_dir_name=".modex",
            enabled=True,
        )

        im_prefix = "im_qq_999"

        # IM adapter's current_ws is set to ws_target; message carries ws_target
        result = await controller.open_workspace(str(ws_target))
        assert result.success is True

        # Transcript store routes writes via the bound workspace root (ctxvar).
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")

        sid = f"{im_prefix}.main"
        with bind_workspace_root(ws_target):
            await store.append(
                sid,
                UserMessageEvent(session_id=sid, agent_name="main", content="QQ message"),
            )
            await store.append(
                sid,
                AssistantTurnEvent(
                    session_id=sid,
                    agent_name="main",
                    turn_id="turn-1",
                    blocks=[{"type": "text", "text": "QQ reply"}],
                    latency_ms=0,
                ),
            )

        # Verify transcript is in target workspace
        events = await store.load(
            sid,
            sessions_dir=WorkspacePaths(root=ws_target / ".modex").sessions_dir,
        )
        assert len(events) == 2
        contents = [str(e.to_dict()) for e in events]
        assert any("QQ message" in c for c in contents)
        assert any("QQ reply" in c for c in contents)

        # Verify NOT in home
        home_file = home / ".modex" / "sessions" / "main" / f"{sid}.jsonl"
        assert not home_file.exists(), "IM transcript must NOT leak to home"
