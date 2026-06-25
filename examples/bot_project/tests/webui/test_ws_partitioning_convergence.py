"""Regression tests for ws-partitioning convergence (risks B and C).

Risk B: the shared WebSocketInputAdapter multiplexes every workspace/tab. The
queue watcher must only bind a dynamically-created delta queue to the
connection that OWNS that conversation, so a subagent stream from one
workspace never lands on another connection.

Risk C: the workspace root ctxvar is the single source of truth for which
workspace a write belongs to. A transcript append / session-index save that
runs with NO bound root silently lands under home/cwd — a latent leak. The
stores now log a loud [ws-partition] warning so any out-of-turn writer is
immediately diagnosable.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import DeltaEnvelope, UserMessageEvent, _unwrap_envelope
from bot.webui.server import WebUIServer
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.workspace.runtime import bind_workspace_root

_DATA_DIR_NAME = ".modex"


# ── Risk B: watcher only claims queues owned by the connection ────────────────


class TestQueueOwnership:
    def test_owned_subagent_prefix_is_claimed(self) -> None:
        # Connection attached convA.main (+ pool agents); a later subagent
        # invocation queue shares the convA prefix → owned.
        attached = ["convA.main", "convA.coding"]
        assert WebUIServer._queue_belongs_to_connection(attached, "convA.main.inv1")
        assert WebUIServer._queue_belongs_to_connection(attached, "convA.helper.z9")

    def test_foreign_conversation_prefix_is_not_claimed(self) -> None:
        # A queue from a different conversation must NOT be claimed by this
        # connection — that's the cross-workspace leak we prevent.
        attached = ["convA.main"]
        assert not WebUIServer._queue_belongs_to_connection(attached, "convB.main")
        assert not WebUIServer._queue_belongs_to_connection(
            attached, "convB.main.inv1"
        )

    def test_empty_attach_state_claims_nothing(self) -> None:
        assert not WebUIServer._queue_belongs_to_connection([], "convA.main")


def _build_server(home: Path) -> WebUIServer:
    inp = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR_NAME)
    store.set_agent_pool_map({"main": "main", "coding": "coding"})
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        data_dir=home,
        home_sessions_dir=WorkspacePaths(root=home / _DATA_DIR_NAME).sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(_DATA_DIR_NAME)
    server.set_agent_pool_map({"main": "main", "coding": "coding"})
    server.set_pool_agent_names(["main", "coding"])
    server.set_session_factory(SessionIdFactory())
    server.set_session_store(
        WorkspacePoolSessionStore(
            base_dir=home,
            pool_resolver=lambda s: "main",
        )
    )
    return server


@pytest.mark.asyncio
async def test_watcher_does_not_forward_foreign_conversation_deltas() -> None:
    """Two connections on one server (shared adapter), each owning its own
    conversation. A subagent delta stream for convA must reach ONLY connA."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        server = _build_server(home)

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            conn_a = await client.ws_connect("/ws")
            await conn_a.send_json(
                {"action": "attach", "uuid_prefix": "convA", "pool": "main"}
            )
            assert _unwrap_envelope(await conn_a.receive_json())["event"] == "attached"

            conn_b = await client.ws_connect("/ws")
            await conn_b.send_json(
                {"action": "attach", "uuid_prefix": "convB", "pool": "main"}
            )
            assert _unwrap_envelope(await conn_b.receive_json())["event"] == "attached"

            # Auto-create a subagent delta queue for convA (as send_envelope does)
            # and push several deltas.
            sub_sid = "convA.main.inv1"
            q = server._input.ensure_queue(sub_sid)
            for i in range(3):
                q.put_nowait(
                    DeltaEnvelope.content(
                        session_id=sub_sid, agent_name="main", text=f"delta-{i}"
                    )
                )

            # Give both watchers a tick (they sleep 1.0s).
            await asyncio.sleep(1.4)

            # connA should have received all three; connB should have none.
            a_deltas: list[str] = []
            for _ in range(3):
                try:
                    env = await conn_a.receive_json(timeout=1)
                    a_deltas.append(str(env.get("event")))
                except asyncio.TimeoutError:
                    break
            assert len(a_deltas) == 3, (
                f"connA should receive all 3 convA deltas, got {len(a_deltas)}"
            )

            try:
                leaked = await conn_b.receive_json(timeout=0.3)
            except asyncio.TimeoutError:
                leaked = None
            assert leaked is None, (
                f"connB must NOT receive convA deltas (cross-workspace leak); "
                f"got {leaked}"
            )

            await conn_a.close()
            await conn_b.close()
        finally:
            await client.close()


# ── Risk C: out-of-turn writes warn loudly instead of leaking to home ─────────


@pytest.mark.asyncio
async def test_transcript_append_warns_when_ws_root_unbound(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR_NAME)
    store.set_agent_pool_map({"main": "main"})
    sid = "convZ.main"
    with caplog.at_level(logging.WARNING, logger="bot.service.workspace_store"):
        # No bind_workspace_root → unbound.
        store.append(sid, UserMessageEvent(session_id=sid, agent_name="main", content="x"))
    assert any("[ws-partition]" in r.message for r in caplog.records), (
        "unbound append must log a [ws-partition] warning"
    )


@pytest.mark.asyncio
async def test_transcript_append_silent_when_ws_root_bound(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR_NAME)
    store.set_agent_pool_map({"main": "main"})
    sid = "convZ.main"
    with caplog.at_level(logging.WARNING, logger="bot.service.workspace_store"):
        with bind_workspace_root(tmp_path):
            store.append(
                sid, UserMessageEvent(session_id=sid, agent_name="main", content="x")
            )
    assert not any("[ws-partition]" in r.message for r in caplog.records), (
        "bound append must NOT warn"
    )


@pytest.mark.asyncio
async def test_session_index_save_warns_when_ws_root_unbound(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    from modex_agent.core.session_id import SessionInfo, now_ms

    index = WorkspacePoolSessionStore(
        base_dir=tmp_path, pool_resolver=lambda s: "main"
    )
    session = SessionInfo(
        session_id="convZ.main",
        agent_name="main",
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    with caplog.at_level(logging.WARNING, logger="bot.service.session_store"):
        await index.save(session)  # no index_dir, no bound root
    assert any("[ws-partition]" in r.message for r in caplog.records), (
        "unbound session-index save must log a [ws-partition] warning"
    )


@pytest.mark.asyncio
async def test_session_index_save_silent_when_index_dir_given(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    from modex_agent.core.session_id import SessionInfo, now_ms

    index = WorkspacePoolSessionStore(
        base_dir=tmp_path, pool_resolver=lambda s: "main"
    )
    session = SessionInfo(
        session_id="convZ.main",
        agent_name="main",
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    with caplog.at_level(logging.WARNING, logger="bot.service.session_store"):
        await index.save(session, index_dir=tmp_path / "idx")  # explicit → no warn
    assert not any("[ws-partition]" in r.message for r in caplog.records), (
        "save with explicit index_dir must NOT warn"
    )
