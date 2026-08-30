"""Tests for WebUIServer REST API and WebSocket handler."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.emitter import WebBotEmitter
from bot.webui.events import DeltaEnvelope, _unwrap_envelope
from bot.webui.server import (
    WebUIServer,
    _new_uuid_prefix,
    _safe_send_json,
)

from modex_agent.core.emitter import AgentResult, EmitterConfig
from modex_agent.multi_agent.pool_router import PoolSessionStore
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.runtime import bind_workspace_root


async def _post_json(
    client: TestClient, path: str, body: dict[str, object] | None
) -> dict[str, object]:
    """Helper: POST JSON to the test client and return parsed response."""
    if body is not None:
        resp = await client.post(path, json=body)
    else:
        resp = await client.post(path)
    assert resp.status == 200
    return await resp.json()


@pytest.mark.asyncio
async def test_api_sessions_list_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions")
            assert resp.status == 200
            data = await resp.json()
            assert data == []
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_send_message_echoes_user_message() -> None:
    """After send_message, the server MUST echo a user_message event back."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        from tests.webui._pipeline_fixture import attach_default_pipeline
        await attach_default_pipeline(
            server, store, input_adapter, workspace_root=workspace_root
        )
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            # Attach to a session
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            # Send a message
            await ws.send_json({"action": "send_message", "session_id": "web:test.main", "content": "hello"})

            # Should receive user_message echoed back
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message", f"Expected user_message, got {echoed['event']}"
            assert echoed["session_id"] == "web:test.main"
            assert echoed["content"] == "hello"
            assert echoed["agent_name"] == "main"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_send_message_passes_attachments_into_envelope() -> None:
    """send_message MUST read ``attachments`` from the client payload and build
    AttachmentRefs on the UserInputEnvelope — otherwise uploaded files are
    orphaned (the ingest stage no-ops on an empty list). Mirrors the QQ
    adapter (bot/adapters/qq.py). Regression for the G8 WS wire fix.
    """
    from modex_agent.input_pipeline.envelope import AttachmentRef

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        from tests.webui._pipeline_fixture import attach_default_pipeline
        await attach_default_pipeline(
            server, store, input_adapter, workspace_root=workspace_root
        )

        # Wrap the pipeline's handle() to capture the envelope that reaches the
        # ingest stage, so the assertion is deterministic without wiring a
        # MediaStore (the ingest stage no-ops cleanly when none is wired).
        captured: list = []
        real_handle = server._input_pipeline.handle

        async def _capture_handle(envelope, ctx):
            captured.append(envelope)
            return await real_handle(envelope, ctx)

        server._input_pipeline.handle = _capture_handle  # type: ignore[method-assign]

        # C1: only local_paths INSIDE the workspace's media staging dir are
        # accepted; create the uploaded temp files there so the realistic
        # upload→WS path is exercised (paths outside staging are dropped).
        staging = (
            WorkspacePaths(root=workspace_root / ".modex").media_dir("main") / "_tmp"
        )
        staging.mkdir(parents=True, exist_ok=True)
        abc_png = staging / "abc.png"
        abc_png.write_bytes(b"png")
        notes_txt = staging / "notes.txt"
        notes_txt.write_bytes(b"notes")

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            await ws.send_json({
                "action": "send_message",
                "session_id": "web:test.main",
                "content": "see this file",
                "attachments": [
                    {"local_path": str(abc_png), "filename": "pic.png", "mime": "image/png"},
                    {"local_path": str(notes_txt)},
                    {"filename": "no-path-should-be-dropped"},
                    "not-a-dict",
                ],
            })
            # Drain the echoed user_message so the handler completes.
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"
        finally:
            await client.close()

        assert len(captured) == 1
        envelope = captured[0]
        assert [a.local_path for a in envelope.attachments] == [
            str(abc_png),
            str(notes_txt),
        ]
        assert isinstance(envelope.attachments[0], AttachmentRef)
        assert envelope.attachments[0].filename == "pic.png"
        assert envelope.attachments[0].mime_type == "image/png"
        # Entries without local_path / wrong type are dropped, never crash.
        assert envelope.attachments[1].filename is None
        assert envelope.attachments[1].mime_type is None


@pytest.mark.asyncio
async def test_ws_send_message_drops_local_path_outside_staging() -> None:
    """C1 (path-traversal fix): a client-supplied ``local_path`` that does NOT
    resolve under the workspace's media staging dir is dropped, never reaches
    the envelope. Without this the WS adapter would build an AttachmentRef for
    ANY server-readable path (e.g. ``/etc/passwd`` or a file under another
    workspace's data dir) and the ingest stage would copy its bytes into the
    media store — exfiltration. The staging dir is the only place the upload
    endpoint writes, so anything outside it is illegitimate.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        from tests.webui._pipeline_fixture import attach_default_pipeline
        await attach_default_pipeline(
            server, store, input_adapter, workspace_root=workspace_root
        )

        # An in-staging file is the legitimate upload path; an outside-staging
        # file simulates the attacker's traversal target. Both exist on disk so
        # the test distinguishes "dropped by the validation" from "dropped
        # because missing" — only the outside one must be rejected.
        staging = (
            WorkspacePaths(root=workspace_root / ".modex").media_dir("main") / "_tmp"
        )
        staging.mkdir(parents=True, exist_ok=True)
        inside = staging / "legit.png"
        inside.write_bytes(b"png")
        outside = workspace_root / "secret" / "etc_passwd"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"root:x:0:0:root:/root:/bin/bash")

        captured: list = []
        real_handle = server._input_pipeline.handle

        async def _capture_handle(envelope, ctx):
            captured.append(envelope)
            return await real_handle(envelope, ctx)

        server._input_pipeline.handle = _capture_handle  # type: ignore[method-assign]

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": "web:c1.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            await ws.send_json({
                "action": "send_message",
                "session_id": "web:c1.main",
                "content": "exfil",
                "attachments": [
                    {"local_path": str(inside), "filename": "legit.png"},
                    {"local_path": str(outside), "filename": "passwd"},
                    # A traversal attempt via ``..`` that resolves outside staging.
                    {"local_path": str(staging / ".." / ".." / "secret" / "etc_passwd")},
                ],
            })
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"
        finally:
            await client.close()

        assert len(captured) == 1
        envelope = captured[0]
        # Only the in-staging entry survives; the outside file and the ``..``
        # traversal are both rejected. No crash, and ``outside`` is never read.
        assert [a.local_path for a in envelope.attachments] == [str(inside)]


@pytest.mark.asyncio
async def test_ws_send_message_echo_carries_resolved_attachments() -> None:
    """The optimistic ``user_message`` echo MUST carry the resolved Attachment
    records so the sender's own attachments render mid-session (symmetric
    rendering), not only after a transcript reload. Mirrors
    persist_user_message.py:43. Regression for the G8 echo fix.
    """
    from modex_agent.media.models import Attachment, AttachmentLocator, Kind

    record = Attachment(
        id="att-1",
        kind=Kind.IMAGE,
        name="pic.png",
        mime="image/png",
        size=1234,
        path=".modex/media/main/uploads/pic.png",
        locator=AttachmentLocator.MEDIA,
    )

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        from tests.webui._pipeline_fixture import attach_default_pipeline
        await attach_default_pipeline(
            server, store, input_adapter, workspace_root=workspace_root
        )

        # The fixture wires no MediaStore, so the ingest stage no-ops and
        # resolved_attachments stays empty. Simulate a successful ingest by
        # appending the record to the envelope AFTER the real pipeline runs —
        # the echo reads final.resolved_attachments off the returned envelope.
        real_handle = server._input_pipeline.handle

        async def _inject_handle(envelope, ctx):
            result = await real_handle(envelope, ctx)
            if result.should_continue():
                result.envelope().resolved_attachments.append(record)
            return result

        server._input_pipeline.handle = _inject_handle  # type: ignore[method-assign]

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            await ws.send_json({
                "action": "send_message",
                "session_id": "web:test.main",
                "content": "see this file",
                "attachments": [
                    {"local_path": "/tmp/uploads/pic.png", "filename": "pic.png", "mime": "image/png"},
                ],
            })
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"
            attachments = echoed.get("attachments")
            assert isinstance(attachments, list)
            assert len(attachments) == 1
            serialized = attachments[0]
            assert serialized == record.to_dict()
            assert serialized["id"] == "att-1"
            assert serialized["kind"] == "image"
            assert serialized["locator"] == "media"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_pause_sends_cancel_turn() -> None:
    """WebSocket pause action sends CANCEL_TURN via the configured control filter."""
    from modex_agent.commands.handlers import build_default_builtin_handlers
    from modex_agent.commands.processor import SlashCommandProcessor
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.control.types import ControlCommandType, ControlScope

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        channel = InMemoryControlChannel()
        processor = SlashCommandProcessor(handlers=list(build_default_builtin_handlers()))
        input_adapter.configure_control_filter(
            control_channel=channel,
            command_processor=processor,
            output_adapter=None,
        )
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        from tests.webui._pipeline_fixture import attach_default_pipeline
        await attach_default_pipeline(
            server, store, input_adapter, workspace_root=workspace_root
        )
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            await ws.send_json({"action": "pause", "session_id": "web:test.main"})
            # Give the async handler a chance to run.
            await asyncio.sleep(0.05)

            cmds = await channel.drain(
                ControlScope(session_id="web:test.main"),
                command_types={ControlCommandType.CANCEL_TURN},
            )
            assert len(cmds) == 1
            assert cmds[0].type == ControlCommandType.CANCEL_TURN
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_pause_reports_error_when_not_handled() -> None:
    """When the control filter is not wired (so /stop is not handled), the
    pause action must surface an error envelope to the client instead of
    silently doing nothing.

    Regression: _ws_pause discarded the _try_intercept_control return value,
    so a misconfigured filter left the pause button with zero feedback.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        # No configure_control_filter() -> _try_intercept_control returns False.
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        from tests.webui._pipeline_fixture import attach_default_pipeline
        await attach_default_pipeline(
            server, store, input_adapter, workspace_root=workspace_root
        )
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            assert _unwrap_envelope(await ws.receive_json())["event"] == "attached"

            await ws.send_json({"action": "pause", "session_id": "web:test.main"})

            env = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert env["event"] == "error", (
                f"pause with no control filter should surface an error, got {env['event']}"
            )
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_api_messages_loads_transcript() -> None:
    """GET /api/sessions/{session_id}/messages returns stored events."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        # Append to the server's active workspace store, not the legacy one.
        from bot.webui.events import UserMessageEvent
        with bind_workspace_root(workspace_root):
            await server._store.append(
                "abc123.main",
                UserMessageEvent(session_id="abc123.main", agent_name="main", content="hello")
)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions/abc123.main/messages")
            assert resp.status == 200
            data = await resp.json()
            assert len(data) == 1
            assert data[0]["event"] == "user_message"
            assert data[0]["content"] == "hello"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_no_static_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/webui/")
            assert resp.status == 503
        finally:
            await client.close()


# ── Pool-per-conversation tests (T3) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_sessions_list_includes_pool() -> None:
    """GET /api/sessions returns one entry per session with session_id and pool."""
    from bot.service.session_store import WorkspacePoolSessionStore

    from modex_agent.core.session_id import SessionIdFactory

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)

    server.set_pool_agent_names(["main", "coding"])
    pool_by_agent = {"coding": "coding", "main": "main"}
    routing_store = PoolSessionStore(data_dir / ".modex")
    server.set_pool_switch_callback(routing_store.set)
    server.set_pool_resolver(routing_store.get_pool)
    factory = SessionIdFactory()
    session_store = WorkspacePoolSessionStore(
        base_dir=WorkspacePaths(root=data_dir / ".modex").session_index_dir,
        pool_resolver=lambda s: pool_by_agent.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)
    server.set_session_factory(factory)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Create sessions via WS attach
        ws = await client.ws_connect("/ws")

        prefix1 = _new_uuid_prefix()
        await ws.send_json({"action": "attach", "uuid_prefix": prefix1, "pool": "coding"})
        attached1 = _unwrap_envelope(await ws.receive_json())
        assert attached1["event"] == "attached"
        s1_sid = attached1["session_id"]

        prefix2 = _new_uuid_prefix()
        await ws.send_json({"action": "attach", "uuid_prefix": prefix2, "pool": "main"})
        attached2 = _unwrap_envelope(await ws.receive_json())
        assert attached2["event"] == "attached"
        s2_sid = attached2["session_id"]

        # Let fire-and-forget session saves complete.
        await asyncio.sleep(0.1)

        # Add transcript data to the server's workspace-scoped store.
        from bot.webui.events import UserMessageEvent
        with bind_workspace_root(data_dir):
            await server._store.append(s1_sid,
                UserMessageEvent(session_id=s1_sid, agent_name="coding", content="hi"),
                pool="coding")
            await server._store.append(s2_sid,
                UserMessageEvent(session_id=s2_sid, agent_name="main", content="hi"))

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {s["session_id"]: s for s in sessions}
        assert s1_sid in by_sid
        assert by_sid[s1_sid]["pool"] == "coding"
        assert s2_sid in by_sid
        assert by_sid[s2_sid]["pool"] == "main"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_session_cleans_up_metadata() -> None:
    """DELETE /api/sessions/{session_id} removes the session transcript."""
    from bot.webui.events import UserMessageEvent

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)

    server.set_pool_agent_names(["main", "coding"])
    routing_store = PoolSessionStore(data_dir / ".modex")
    server.set_pool_switch_callback(routing_store.set)
    server.set_pool_resolver(routing_store.get_pool)

    from bot.service.session_gc import SessionGarbageCollector, SessionGcConfig

    server.set_session_gc(
        SessionGarbageCollector(
            workspace_roots_provider=lambda: [data_dir],
            data_dir_name=".modex",
            config=SessionGcConfig(),
        )
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        prefix = _new_uuid_prefix()
        await ws.send_json({"action": "attach", "uuid_prefix": prefix, "pool": "coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"
        session_id = attached["session_id"]

        # Write a transcript so there is something to delete.
        with bind_workspace_root(data_dir):
            await store.append(
                session_id,
                UserMessageEvent(session_id=session_id, agent_name="coding", content="test"),
                pool="coding",
            )
        transcript_file = data_dir / ".modex" / "sessions" / "coding" / f"{session_id}.jsonl"
        assert transcript_file.exists()

        resp = await client.delete(f"/api/sessions/{session_id}")
        assert resp.status == 200
        assert not transcript_file.exists(), "transcript file was not removed"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_session_removes_transcript_from_any_pool_directory() -> None:
    """Retired: wrong-pool routing is a fixed bug, and the foreground delete now
    removes the transcript by the resolved (correct) pool. Orphan transcripts
    (no index, any pool) are recovered by the sweep — covered in
    tests/service/test_session_gc.py::test_sweep_catches_orphan_transcript.
    """
    pass


@pytest.mark.asyncio
async def test_delete_session_delegates_to_collector() -> None:
    """DELETE /api/sessions/{id} delegates to the collector with resolved pool."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])

    class FakeGC:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        async def delete_session_tree(self, root_session_id, ws_root=None, pool=None) -> bool:
            self.calls.append((root_session_id, pool))
            return True

    fake = FakeGC()
    server.set_session_gc(fake)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.delete("/api/sessions/abc.main")
        assert resp.status == 200
        assert await resp.json() == {"deleted": "abc.main"}
        assert fake.calls == [("abc.main", "main")]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_send_message_uses_stored_pool() -> None:
    """WebSocket send_message uses pool from PoolRouter resolver, not client data."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)

    # Wire resolver so the send path derives agent_name from pool_name.
    server.set_pool_resolver(lambda cid: "coding")

    from unittest.mock import MagicMock

    from tests.webui._pipeline_fixture import attach_default_pipeline
    pool_store = MagicMock()
    pool_store.get = lambda key, default=None: "coding"
    pool_store.set = MagicMock()
    await attach_default_pipeline(
        server, store, input_adapter, pool_session_store=pool_store, workspace_root=data_dir
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "session_id": "web:test-pool.coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Send message WITHOUT pool field — server reads from stored mapping
        await ws.send_json({
            "action": "send_message",
            "session_id": "web:test-pool.coding",
            "content": "hello from coding pool",
        })

        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert echoed["event"] == "user_message"
        assert echoed["agent_name"] == "coding", (
            f"Expected agent_name='coding' from stored pool, "
            f"got {echoed['agent_name']}"
        )
        assert echoed["content"] == "hello from coding pool"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_attach_restores_pool_routing() -> None:
    """WebSocket attach calls pool_switch_callback with stored pool."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])
    server.set_pool_resolver(lambda cid: "coding")

    # Set a mock callback to verify it is called
    callback = MagicMock()
    server.set_pool_switch_callback(callback)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "session_id": "web:test-attach.coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Callback should have been invoked with the snowflake (agent-independent id) and pool_name
        callback.assert_called_once_with("web:test-attach", "coding")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pool_mapping_persistence_across_restart() -> None:
    """Pool mapping survives server restart via physical transcript layout."""
    from bot.service.session_store import WorkspacePoolSessionStore

    from modex_agent.core.session_id import SessionInfo, now_ms

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    pool_by_agent = {"main": "main", "coding": "coding"}
    routing_store = PoolSessionStore(data_dir / ".modex")

    # First server instance — create a session in the coding pool and send a
    # message so the transcript is persisted to disk (empty sessions are not).
    server1 = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server1.set_workspace_index(store)
    server1.set_pool_agent_names(["main", "coding"])
    server1.set_pool_switch_callback(routing_store.set)
    server1.set_pool_resolver(routing_store.get_pool)
    session_store1 = WorkspacePoolSessionStore(
        base_dir=WorkspacePaths(root=data_dir / ".modex").session_index_dir,
        pool_resolver=lambda s: pool_by_agent.get(s.agent_name, "main"),
    )
    server1.set_session_store(session_store1)
    from tests.webui._pipeline_fixture import attach_default_pipeline
    await attach_default_pipeline(
        server1,
        store,
        input_adapter,
        pool_session_store=routing_store,
        workspace_root=data_dir,
    )
    client1 = TestClient(TestServer(server1.app))
    await client1.start_server()
    try:
        conv_id = _new_uuid_prefix()
        ws = await client1.ws_connect("/ws")
        await ws.send_json({"action": "attach", "uuid_prefix": conv_id, "pool": "coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"
        session_id = attached["session_id"]

        # Save session to the session store so server2 can list it.
        await session_store1.save(SessionInfo(
            session_id=session_id,
            agent_name="coding",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

        await ws.send_json({
            "action": "send_message",
            "session_id": session_id,
            "content": "hello coding",
        })
        _unwrap_envelope(await ws.receive_json(timeout=2))
    finally:
        await client1.close()

    # Verify transcript file exists under the coding pool directory.
    transcript_file = data_dir / ".modex" / "sessions" / "coding" / f"{conv_id}.coding.jsonl"
    assert transcript_file.exists()

    # No sessions.json in the new design.
    meta_file = data_dir / ".modex" / "sessions.json"
    assert not meta_file.exists()

    # Second server instance — fresh store scanning the same disk layout.
    store2 = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server2 = WebUIServer(
        input_adapter, store2, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server2.set_workspace_index(store2)
    server2.set_pool_agent_names(["main", "coding"])
    server2.set_pool_resolver(routing_store.get_pool)
    session_store2 = WorkspacePoolSessionStore(
        base_dir=WorkspacePaths(root=data_dir / ".modex").session_index_dir,
        pool_resolver=lambda s: pool_by_agent.get(s.agent_name, "main"),
    )
    server2.set_session_store(session_store2)
    client2 = TestClient(TestServer(server2.app))
    await client2.start_server()
    try:
        resp = await client2.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {s["session_id"]: s for s in sessions}
        assert session_id in by_sid, (
            f"Session {session_id!r} must survive server restart."
        )
        assert by_sid[session_id]["pool"] == "coding"
    finally:
        await client2.close()


@pytest.mark.asyncio
async def test_sessions_persist_across_pool_switch_and_qq_conversation() -> None:
    """Regression: sessions must not disappear after pool switch + QQ chat.

    Bug report: User switches pool to coding, chats, then uses QQ. After
    switching back to main the list is empty; switching back to coding is
    also empty. This test pins the backend contract.
    """
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.webui.events import UserMessageEvent

    from modex_agent.core.session_id import SessionInfo, now_ms

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])

    pool_by_agent = {"main": "main", "coding": "coding"}
    routing_store = PoolSessionStore(data_dir / ".modex")
    server.set_pool_switch_callback(routing_store.set)
    server.set_pool_resolver(routing_store.get_pool)
    session_store = WorkspacePoolSessionStore(
        base_dir=WorkspacePaths(root=data_dir / ".modex").session_index_dir,
        pool_resolver=lambda s: pool_by_agent.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)

    from tests.webui._pipeline_fixture import attach_default_pipeline
    await attach_default_pipeline(
        server,
        store,
        input_adapter,
        pool_session_store=routing_store,
        workspace_root=data_dir,
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        coding_conv = _new_uuid_prefix()
        coding_sid = f"{coding_conv}.coding"

        ws = await client.ws_connect("/ws")
        await ws.send_json(
            {"action": "attach", "uuid_prefix": coding_conv, "pool": "coding"}
        )
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Save coding session to the session store.
        await session_store.save(SessionInfo(
            session_id=coding_sid,
            agent_name="coding",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

        await ws.send_json(
            {
                "action": "send_message",
                "session_id": coding_sid,
                "content": "hello coding",
            }
        )
        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert echoed["event"] == "user_message"
        assert echoed["agent_name"] == "coding"

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {str(s["session_id"]): s for s in sessions}
        assert coding_sid in by_sid
        assert by_sid[coding_sid]["pool"] == "coding"

        qq_conv_id = "qq:group:12345"
        qq_sid = f"{qq_conv_id}.main"
        routing_store.set(qq_conv_id, "main")
        with bind_workspace_root(data_dir):
            await server._store.append(
                qq_sid,
                UserMessageEvent(
                    session_id=qq_sid,
                    agent_name="main",
                    content="hello from QQ"
)
)

        # Save QQ session to the session store.
        await session_store.save(SessionInfo(
            session_id=qq_sid,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        by_sid = {str(s["session_id"]): s for s in sessions}
        assert coding_sid in by_sid, (
            f"coding session disappeared after QQ conversation; "
            f"sessions={sessions}"
        )
        assert by_sid[coding_sid]["pool"] == "coding"
        assert coding_sid in by_sid

        main_conv = _new_uuid_prefix()
        main_sid = f"{main_conv}.main"
        await ws.send_json(
            {"action": "attach", "uuid_prefix": main_conv, "pool": "main"}
        )
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"
        # Save main session to the session store.
        await session_store.save(SessionInfo(
            session_id=main_sid,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))
        await ws.send_json(
            {
                "action": "send_message",
                "session_id": main_sid,
                "content": "hello main",
            }
        )
        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert echoed["event"] == "user_message"

        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        by_sid = {str(s["session_id"]): s for s in sessions}
        assert coding_sid in by_sid
        assert by_sid[coding_sid]["pool"] == "coding"
        assert main_sid in by_sid
        assert by_sid[main_sid]["pool"] == "main"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_attach_switches_all_sessions() -> None:
    """Switching sessions must unregister ALL previous sessions.

    Regression: _ws_attach only unregistered the main session and cancelled
    its delta task, but left pool-agent / subagent sessions from the previous
    session registered.  Those sessions kept forwarding deltas to the
    same WebSocket, causing the frontend to render another session's
    streaming output after switching.
    """
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")

        # Attach to session A
        await ws.send_json({"action": "attach", "session_id": "web:conv-a.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Server registers main.A and coding.A for this WebSocket
        assert "web:conv-a.main" in input_adapter._delta_queues
        assert "web:conv-a.coding" in input_adapter._delta_queues

        # Now switch to session B
        await ws.send_json({"action": "attach", "session_id": "web:conv-b.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Previous session sessions must be fully unregistered
        assert "web:conv-a.main" not in input_adapter._delta_queues
        assert "web:conv-a.coding" not in input_adapter._delta_queues
        assert "web:conv-a.main" not in input_adapter._delta_queues
        assert "web:conv-a.coding" not in input_adapter._delta_queues

        # New session sessions must be registered
        assert "web:conv-b.main" in input_adapter._delta_queues
        assert "web:conv-b.coding" in input_adapter._delta_queues
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sessions_list_includes_subagent_with_parent_relation() -> None:
    """GET /api/sessions includes subagent sessions that have parent relationships."""
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.webui.events import UserMessageEvent

    from modex_agent.core.session_id import SessionInfo

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding", "reviewer"])

    # Create session store and record parent→child via SessionInfo
    parent_sid = "abc.coding"
    child_sid = "abc.coding.reviewer.ee11"
    session_store = WorkspacePoolSessionStore(
        WorkspacePaths(root=data_dir / ".modex").session_index_dir,
        pool_resolver=lambda s: "coding",
    )
    server.set_pool_resolver(lambda session_prefix: "coding" if session_prefix == "abc" else None)
    parent_session = SessionInfo(
        session_id=parent_sid, agent_name="coding"
)
    child_session = SessionInfo(
        session_id=child_sid, agent_name="reviewer",
        parent_session_id=parent_sid
)
    await session_store.save(parent_session)
    await session_store.save(child_session)
    server.set_session_store(session_store)

    # Add transcript data for both parent and child
    with bind_workspace_root(data_dir):
        await store.append(parent_sid,
            UserMessageEvent(session_id=parent_sid, agent_name="coding", content="hi"),
            pool="coding")
        await store.append(child_sid,
            UserMessageEvent(session_id=child_sid, agent_name="reviewer", content="reviewing"),
            pool="coding")

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {s["session_id"]: s for s in sessions}

        # Parent session should be present
        assert parent_sid in by_sid, f"parent {parent_sid} missing from sessions"
        assert by_sid[parent_sid]["parent_session_id"] is None

        # Subagent session should ALSO be present with correct parent
        assert child_sid in by_sid, (
            f"child {child_sid} missing from sessions — "
            "subagent sessions with parent relations must be included"
        )
        assert by_sid[child_sid]["parent_session_id"] == parent_sid
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_messages_loads_subagent_transcript() -> None:
    """GET /api/sessions/{subagent_id}/messages loads subagent transcript events."""
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.webui.events import UserMessageEvent

    from modex_agent.core.session_id import SessionInfo, now_ms

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["coding"])

    # Session store so _resolve_agent finds correct agent_name for subagent.
    session_store = WorkspacePoolSessionStore(
        base_dir=WorkspacePaths(root=data_dir / ".modex").session_index_dir,
        pool_resolver=lambda s: "coding",
    )
    server.set_pool_resolver(lambda session_prefix: "coding" if session_prefix == "abc" else None)
    server.set_session_store(session_store)

    parent_sid = "abc.coding"
    child_sid = "abc.coding.reviewer.ee11"

    # Write transcript data for the subagent session
    with bind_workspace_root(data_dir):
        await store.append(parent_sid,
            UserMessageEvent(session_id=parent_sid, agent_name="coding", content="hi"),
            pool="coding")
        await store.append(child_sid,
            UserMessageEvent(session_id=child_sid, agent_name="reviewer", content="review result"),
            pool="coding")

    # Save subagent session to the store so _resolve_agent finds "reviewer".
    await session_store.save(SessionInfo(
        session_id=child_sid,
        agent_name="reviewer",
        created_at=now_ms(),
        updated_at=now_ms(),
    ))

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Load messages for the subagent session
        resp = await client.get(f"/api/sessions/{child_sid}/messages")
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        events = await resp.json()
        assert len(events) == 2, (
            f"Expected 2 events (all sessions sharing prefix), got {len(events)}: {events}"
        )
        child_events = [e for e in events if e["session_id"] == child_sid]
        assert len(child_events) == 1, f"Expected 1 subagent event, got {child_events}"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_subagent_streaming_delta_arrives_at_ws_client() -> None:
    """Subagent emitter deltas must arrive at the WebSocket client via watcher."""
    from bot.webui.events import SessionMeta

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["coding"])

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")

        parent_sid = "sub.coding"
        child_sid = "sub.coding.reviewer.ee11"

        # Attach to the parent session
        await ws.send_json({"action": "attach", "session_id": parent_sid})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Simulate what _on_subagent_created does: pre-register the subagent queue
        input_adapter.ensure_queue(child_sid)

        # Create a WebBotEmitter for the subagent (this is what the pipeline does)
        emitter = WebBotEmitter(
            output_adapter,
            child_sid,
            config=EmitterConfig(),
            session_meta_resolver=lambda: SessionMeta(parent_session_id=parent_sid),
        )

        # Emit a delta — this should enqueue into the delta queue
        await emitter.emit_delta("subagent streaming test")

        # The watcher should pick up the new queue within ~1.5s and start forwarding.
        # Wait for the delta to arrive at the WS client.
        received_raw = await ws.receive_json(timeout=3)
        received = _unwrap_envelope(received_raw)
        assert received["event"] == "model_content_delta", (
            f"Expected model_content_delta, got {received.get('event')}"
        )
        assert received["session_id"] == child_sid, (
            f"Expected session_id={child_sid}, got {received.get('session_id')}"
        )
        assert received["text"] == "subagent streaming test"
        # parent_session_id is on the envelope (not unwrapped); check raw
        assert received_raw.get("parent_session_id") == parent_sid
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_full_stream_isolation_across_sessions() -> None:
    """End-to-end: emitter deltas for one session do not leak after switching.

    This exercises the real pipeline:
      WebBotEmitter -> WebSocketOutputAdapter -> per-session queue ->
      WebUIServer._forward_deltas -> WebSocket -> client.
    """
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")

        # Attach to session A
        await ws.send_json({"action": "attach", "session_id": "web:conv-a.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Emit a streaming delta for A through the real emitter
        emitter_a = WebBotEmitter(
            output_adapter,
            "web:conv-a.main",
            config=EmitterConfig()
)
        await emitter_a.emit_delta("hello from A")

        received = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert received["event"] == "model_content_delta"
        assert received["session_id"] == "web:conv-a.main"
        assert received["text"] == "hello from A"

        # Switch to session B
        await ws.send_json({"action": "attach", "session_id": "web:conv-b.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Emit another delta for A — it must NOT arrive on this WebSocket
        await emitter_a.emit_delta("leaked from A")

        # Because A is unregistered, the queue is gone and send_delta is a no-op,
        # so the next receive_json would timeout.  We assert the connection is
        # clean by checking the adapter state directly.
        assert "web:conv-a.main" not in input_adapter._delta_queues

        # Emit for B and confirm it DOES arrive
        emitter_b = WebBotEmitter(
            output_adapter,
            "web:conv-b.main",
            config=EmitterConfig()
)
        await emitter_b.emit_delta("hello from B")

        received = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert received["event"] == "model_content_delta"
        assert received["session_id"] == "web:conv-b.main"
        assert received["text"] == "hello from B"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_turn_end_streaming_stop_is_isolated() -> None:
    """turn_end for session A must not stop streaming in session B."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")

        # Attach two different tabs by using two separate connections is hard
        # in one test; instead we verify the contract at the event level:
        # the frontend reducer ignores turn_end for a different session.
        await ws.send_json({"action": "attach", "session_id": "web:conv-a.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        emitter_a = WebBotEmitter(
            output_adapter,
            "web:conv-a.main",
            config=EmitterConfig()
)
        await emitter_a.emit_delta("streaming in A...")
        delta = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert delta["event"] == "model_content_delta"

        # Send a turn_end for a different session over the same connection.
        # The backend will forward it because the connection is currently attached
        # to A, but the frontend reducer must ignore it.
        await ws.send_str(json.dumps({
            "event": "turn_end",
            "session_id": "web:conv-b.main",
            "agent_name": "main",
            "turn_id": "turn_1",
            "latency_ms": 0,
        }))

        # The real turn_end for A should still end streaming correctly.
        await emitter_a.emit_complete(AgentResult(content="done"))
        end = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert end["event"] == "turn_end"
        assert end["session_id"] == "web:conv-a.main"
    finally:
        await client.close()


# ═══════════════════════════════════════════════════════════════════
# SessionInfo / legacy transcript fallback tests
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ws_attach_existing_session_does_not_crash() -> None:
    """Attaching to an already-created session_id must register the connection
    and proactive pool sessions without referencing an undefined uuid_prefix_raw.

    Regression: the new-conversation branch assigned ``uuid_prefix_raw``, but
    the existing-session branch did not.  The proactive pool-agent loop then
    referenced ``uuid_prefix_raw`` unconditionally and raised
    ``UnboundLocalError``.
    """
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "session_id": "abc123.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"
        assert attached["session_id"] == "abc123.main"

        # Main session and proactive pool sessions must be registered.
        assert "abc123.main" in input_adapter._delta_queues
        assert "abc123.coding" in input_adapter._delta_queues
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_attach_new_conversation_uses_stable_snowflake_for_pool_agents() -> None:
    """When attaching with uuid_prefix + pool, proactive pool-agent session ids
    must share the SAME encoded snowflake as the main session.

    Regression: the loop re-encoded ``uuid_prefix_raw`` via the factory,
    producing a different snowflake for pool-agent queues than the main
    session's transcript/delta queue, so deltas were dropped.
    """
    from modex_agent.core.session_id import SessionIdFactory

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])
    server.set_session_factory(SessionIdFactory())

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        raw_prefix = _new_uuid_prefix()
        await ws.send_json(
            {"action": "attach", "uuid_prefix": raw_prefix, "pool": "coding"}
        )
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"
        main_sid: str = attached["session_id"]
        snowflake = main_sid.split(".", 1)[0]

        expected_coding_sid = f"{snowflake}.coding"
        assert expected_coding_sid in input_adapter._delta_queues, (
            f"Expected pool-agent queue {expected_coding_sid!r}, "
            f"got queues: {list(input_adapter._delta_queues)}"
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_sessions_falls_back_to_transcripts_when_index_empty() -> None:
    """GET /api/sessions derives sessions from transcript files when the
    SessionInfo index has no record for them.

    Regression: legacy workspaces only have ``.modex/sessions/<pool>/*.jsonl``
    files and no ``.modex/session_index/``, so the session list was empty.
    """
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.webui.events import UserMessageEvent

    from modex_agent.core.session_id import SessionIdFactory

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])

    # Empty session store — no SessionInfo index entries yet.
    pool_by_agent = {"coding": "coding", "main": "main"}
    session_store = WorkspacePoolSessionStore(
        base_dir=WorkspacePaths(root=data_dir / ".modex").session_index_dir,
        pool_resolver=lambda s: pool_by_agent.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)
    server.set_session_factory(SessionIdFactory())
    server.set_pool_resolver(
        lambda session_prefix: "coding" if session_prefix == "legacy123" else None
    )

    # Write a legacy transcript directly into the coding pool directory.
    legacy_sid = "legacy123.coding"
    with bind_workspace_root(data_dir):
        await store.append(
            legacy_sid,
            UserMessageEvent(
                session_id=legacy_sid, agent_name="coding", content="hi"
            ),
            pool="coding",
        )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {s["session_id"]: s for s in sessions}
        assert legacy_sid in by_sid, (
            f"Legacy transcript session {legacy_sid!r} missing; "
            f"sessions={sessions}"
        )
        assert by_sid[legacy_sid]["pool"] == "coding"
        assert by_sid[legacy_sid]["parent_session_id"] is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_sessions_falls_back_preserves_index_entries() -> None:
    """When a session exists in BOTH the SessionInfo index and transcripts,
    the index entry wins (richer metadata), and the transcript is not duplicated.
    """
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.webui.events import UserMessageEvent

    from modex_agent.core.session_id import SessionIdFactory, SessionInfo, now_ms

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["coding"])

    pool_by_agent = {"coding": "coding"}
    session_store = WorkspacePoolSessionStore(
        base_dir=WorkspacePaths(root=data_dir / ".modex").session_index_dir,
        pool_resolver=lambda s: pool_by_agent.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)
    server.set_session_factory(SessionIdFactory())
    server.set_pool_resolver(
        lambda session_prefix: "coding" if session_prefix == "idx456" else None
    )

    indexed_sid = "idx456.coding"
    await session_store.save(
        SessionInfo(
            session_id=indexed_sid,
            agent_name="coding",
            parent_session_id=None,
            created_at=now_ms(),
            updated_at=now_ms(),
            metadata={"source": "index"},
        )
    )
    with bind_workspace_root(data_dir):
        await store.append(
            indexed_sid,
            UserMessageEvent(
                session_id=indexed_sid, agent_name="coding", content="hi"
            ),
            pool="coding",
        )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        assert len(sessions) == 1, f"Expected 1 session, got {sessions}"
        assert sessions[0]["session_id"] == indexed_sid
        assert sessions[0].get("metadata", {}).get("source") == "index"
    finally:
        await client.close()


# ═══════════════════════════════════════════════════════════════════
# Workspace API tests
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_workspace_cd_switches_current_workspace() -> None:
    """POST /api/workspace/cd changes the current workspace path.

    With the session-aware store, workspace isolation is achieved by the
    ``bind_workspace_root`` ctxvar, not by global rebase.  This test verifies
    the binding correctly isolates writes.
    """
    from bot.service.session_store import WorkspacePoolSessionStore

    from modex_agent.core.session_id import SessionInfo, now_ms

    home = Path(tempfile.mkdtemp())
    ws_a = home / "ws-a"
    ws_b = home / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()

    sessions_a = ws_a / ".modex" / "sessions"
    sessions_b = ws_b / ".modex" / "sessions"
    sessions_a.mkdir(parents=True)
    sessions_b.mkdir(parents=True)

    # The store routes writes by the bound workspace root (ctxvar); reads
    # accept an explicit ``sessions_dir`` override (used by the server's
    # ``home_sessions_dir``).
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    inp = WebSocketInputAdapter()
    server = WebUIServer(inp, store, static_dist=None, data_dir=home, home_sessions_dir=sessions_a)
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main"])

    # Session store for session listing.
    session_store = WorkspacePoolSessionStore(
        base_dir=sessions_a,
        pool_resolver=lambda s: "main",
    )
    server.set_session_store(session_store)
    server.set_pool_resolver(lambda session_prefix: "main")

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # ── Add a session in workspace A ───────────────────────────
        from bot.webui.events import UserMessageEvent

        sid_a = f"{_new_uuid_prefix()}.main"

        with bind_workspace_root(ws_a):
            await store.append(
                sid_a, UserMessageEvent(session_id=sid_a, agent_name="main", content="ws-a")
            )

        # Save session to session store so it appears in listing.
        await session_store.save(SessionInfo(
            session_id=sid_a,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

        # Verify session is visible in workspace A (home dir listing in Task 1)
        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        assert any(s["session_id"] == sid_a for s in sessions), (
            f"session {sid_a} must be visible in ws-a"
        )

        # ── Add a session in workspace B ──────────────────────────────────
        sid_b = f"{_new_uuid_prefix()}.main"

        with bind_workspace_root(ws_b):
            await store.append(
                sid_b, UserMessageEvent(session_id=sid_b, agent_name="main", content="ws-b")
            )

        await session_store.save(SessionInfo(
            session_id=sid_b,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

        # Verify the binding correctly isolates writes to different dirs.
        assert (sessions_a / "main" / f"{sid_a}.jsonl").exists()
        assert not (sessions_b / "main" / f"{sid_a}.jsonl").exists()
        assert (sessions_b / "main" / f"{sid_b}.jsonl").exists()
        assert not (sessions_a / "main" / f"{sid_b}.jsonl").exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_sessions_includes_subagent_sessions() -> None:
    """GET /api/sessions must return subagent sessions and expose their
    parent_session_id so the frontend can render them under the parent.

    Regression: the endpoint filtered to ``_pool_agent_names`` (main agents
    only), so subagent sessions never appeared and the tree was flat.
    """
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.webui.events import UserMessageEvent

    from modex_agent.core.session_id import SessionInfo, now_ms

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["coding"])

    pool_by_agent = {"coding": "coding", "reviewer": "coding"}
    session_store = WorkspacePoolSessionStore(
        base_dir=WorkspacePaths(root=data_dir / ".modex").session_index_dir,
        pool_resolver=lambda s: pool_by_agent.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)
    server.set_pool_resolver(
        lambda session_prefix: "coding" if session_prefix == "abc" else None
    )

    parent_sid = "abc.coding"
    child_sid = "abc.coding.reviewer.ee11"
    await session_store.save(
        SessionInfo(
            session_id=parent_sid,
            agent_name="coding",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
    )
    await session_store.save(
        SessionInfo(
            session_id=child_sid,
            agent_name="reviewer",
            parent_session_id=parent_sid,
            created_at=now_ms(),
            updated_at=now_ms(),
        )
    )
    with bind_workspace_root(data_dir):
        await store.append(
            child_sid,
            UserMessageEvent(
                session_id=child_sid, agent_name="reviewer", content="review done"
            ),
            pool="coding",
        )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {s["session_id"]: s for s in sessions}

        assert parent_sid in by_sid, f"parent session missing: {sessions}"
        assert child_sid in by_sid, (
            f"subagent session missing; only main-agent sessions returned: {sessions}"
        )
        assert by_sid[child_sid]["parent_session_id"] == parent_sid
        assert by_sid[child_sid]["pool"] == "coding"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_sessions_includes_dynamic_subagent_instance() -> None:
    """Dynamic subagent instances like ``reviewer-abc123`` inherit the pool
    of their template type and must appear in the session list.
    """
    from bot.service.session_store import WorkspacePoolSessionStore

    from modex_agent.core.session_id import SessionInfo, now_ms

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=data_dir / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["coding"])

    pool_by_agent = {"coding": "coding", "reviewer": "coding"}
    session_store = WorkspacePoolSessionStore(
        base_dir=WorkspacePaths(root=data_dir / ".modex").session_index_dir,
        pool_resolver=lambda s: pool_by_agent.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)
    server.set_pool_resolver(
        lambda session_prefix: "coding" if session_prefix == "abc" else None
    )

    dynamic_sid = "abc.coding.reviewer-instance-7"
    await session_store.save(
        SessionInfo(
            session_id=dynamic_sid,
            agent_name="reviewer-instance-7",
            parent_session_id="abc.coding",
            created_at=now_ms(),
            updated_at=now_ms(),
        )
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        sids = {s["session_id"] for s in sessions}
        assert dynamic_sid in sids, (
            f"Dynamic subagent instance not listed; sessions={sessions}"
        )
        by_sid = {s["session_id"]: s for s in sessions}
        assert by_sid[dynamic_sid]["pool"] == "coding"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_attach_starts_forward_deltas_for_main_session() -> None:
    """After attach, the main session MUST have an active _forward_deltas task
    that drains the delta queue and pushes envelopes to the WebSocket client.

    Regression test: _forward_deltas(session_id, ws) was dropped from _ws_attach
    for the main session during the input-pipeline refactor.  Without it, agent
    output is enqueued but never forwarded, so the frontend appears frozen.
    """
    ws_input = WebSocketInputAdapter()
    output = WebSocketOutputAdapter(ws_input)
    workspace_root = Path(tempfile.mkdtemp())
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
    server = WebUIServer(ws_input, store, static_dist=None, home_sessions_dir=home_sessions_dir)
    server.set_workspace_index(store)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        # Attach a main session (not pool agent, not subagent).
        await ws.send_json({"action": "attach", "session_id": "web:main-sess.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Create a WebBotEmitter for the main session and emit a delta.
        from bot.webui.events import SessionMeta
        emitter = WebBotEmitter(
            output, "web:main-sess.main", config=EmitterConfig(),
            pool="main",
            session_meta_resolver=lambda: SessionMeta(parent_session_id=None),
        )
        await emitter.emit_delta("streaming test for main session")

        # The _forward_deltas task (started by _ws_attach) should drain the
        # queue and send the delta to this WS client.
        received_raw = await ws.receive_json(timeout=3)
        received = _unwrap_envelope(received_raw)
        assert received["event"] == "model_content_delta", (
            f"Expected model_content_delta, got {received.get('event')}"
        )
        assert received["session_id"] == "web:main-sess.main"
        assert received["text"] == "streaming test for main session"
    finally:
        await client.close()


# ── Peer-review follow-ups (server.py hardening) ───────────────────────────


@pytest.mark.asyncio
async def test_safe_send_json_swallows_send_errors() -> None:
    """Fire-and-forget send helper must not leak unretrieved task exceptions."""

    class _BrokenWS:
        async def send_json(self, data: dict[str, object]) -> None:
            raise ConnectionResetError("simulated closed socket")

    # Should not raise; helper catches and ignores send failures.
    task = asyncio.create_task(_safe_send_json(_BrokenWS(), {"event": "test"}))
    await task
    assert task.done()


@pytest.mark.asyncio
async def test_cleanup_drains_delta_queues() -> None:
    """_WsConnectionState.cleanup must discard pending deltas before cancelling tasks.

    Prevents a cancelled forward task from consuming deltas intended for an old
    session and sending them to a reused WebSocket connection.
    """
    from bot.webui.server import _WsConnectionState

    input_adapter = WebSocketInputAdapter()
    ws = object()
    input_adapter.register_connection("sess1", ws)
    q = input_adapter.get_delta_queue("sess1", ws)
    assert q is not None
    q.put_nowait(DeltaEnvelope.content(session_id="sess1", agent_name="main", text="old"))

    state = _WsConnectionState()
    state.attached_sessions.append("sess1")
    state.forward_tasks.append(asyncio.create_task(asyncio.sleep(3600)))

    await state.cleanup(input_adapter, ws)

    assert q.empty()
    assert input_adapter.get_delta_queue("sess1", ws) is None
    assert not state.forward_tasks
    assert not state.attached_sessions


@pytest.mark.asyncio
async def test_subagent_invocation_id_matching_agent_name_still_registered() -> None:
    """Regression: subagent session id must not be mis-parsed as a main-agent session.

    ``SessionInfo.from_str('conv.reviewer.main')`` returns agent_name='main'
    (it takes the last segment via rpartition).  A subagent invocation whose
    invocation_id happens to equal a pool agent name must still be registered
    for delta forwarding.  The fix uses ``agent_of()`` to extract the true agent
    segment and segment-count to detect main-agent sessions.
    """
    from bot.webui.events import UserMessageEvent

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir)
        server.set_workspace_index(store)
        server.set_pool_agent_names(["main", "reviewer"])

        # Main session + a subagent invocation whose invocation_id equals "main"
        # (a pool agent name). The old from_str() parsing would skip it.
        with bind_workspace_root(workspace_root):
            await store.append(
                "conv.main",
                UserMessageEvent(session_id="conv.main", agent_name="main", content="hi")
            )
            await store.append(
                "conv.reviewer.main",  # prefix.reviewer.<invocation_id=main>
                UserMessageEvent(session_id="conv.reviewer.main", agent_name="reviewer", content="review")
            )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"action": "attach", "session_id": "conv.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            # The subagent session must have its own delta queue registered.
            assert input_adapter.get_delta_queues("conv.reviewer.main")
        finally:
            await client.close()



# ── Frontend review follow-ups (small correctness fixes) ───────────────────


@pytest.mark.asyncio
async def test_api_messages_sorts_with_none_timestamp() -> None:
    """A malformed/None timestamp must not crash the messages endpoint.

    Regression: ``int(str(None))`` raises ValueError and produced a 500.
    """
    from bot.webui.events import UserMessageEvent

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir)
        server.set_workspace_index(store)
        with bind_workspace_root(workspace_root):
            await server._store.append(
                "abc123.main",
                UserMessageEvent(session_id="abc123.main", agent_name="main", content="hello")
            )

        # Simulate an event whose serialized form has a missing/None timestamp.
        original_to_dict = UserMessageEvent.to_dict

        def _to_dict_with_none_timestamp(self: UserMessageEvent) -> dict[str, object]:
            data = original_to_dict(self)
            data["timestamp"] = None
            return data

        UserMessageEvent.to_dict = _to_dict_with_none_timestamp  # type: ignore[method-assign]
        try:
            client = TestClient(TestServer(server.app))
            await client.start_server()
            try:
                resp = await client.get("/api/sessions/abc123.main/messages")
                assert resp.status == 200
                data = await resp.json()
                assert len(data) == 1
            finally:
                await client.close()
        finally:
            UserMessageEvent.to_dict = original_to_dict  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_workspace_cd_returns_400_on_malformed_json() -> None:
    """Malformed JSON body is rejected with HTTP 400, not silently falling back to home."""
    from modex_agent.workspace.models import CdResult
    from modex_agent.workspace.port import WorkspaceControlPort

    home = Path(tempfile.mkdtemp())

    class _FakeControl(WorkspaceControlPort):
        def current(self, session_id: str) -> Path:
            return home

        @property
        def home(self) -> Path:
            return home

        def pwd(self, session_id: str) -> str:
            return f"cwd: {home}\nhome: {home}"

        async def open_workspace(self, target: str) -> CdResult:
            return CdResult(success=True, current_path=home, original_path=home, notice="ok")

        async def switch(self, session_id: str, target: str) -> CdResult:
            return CdResult(success=True, current_path=home, original_path=home, notice="ok")

        async def exit(self, session_id: str) -> CdResult:
            return await self.switch(session_id, str(home))

    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir)
        server.set_workspace_control(_FakeControl())
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.post("/api/workspace/cd", data="not-json")
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "invalid body"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_create_session_graceful_on_malformed_json() -> None:
    """Malformed JSON body must not crash session creation; it uses default pool."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir)
        server.set_workspace_index(store)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.post("/api/sessions", data="not-json")
            assert resp.status == 200
            data = await resp.json()
            assert data["session_id"].endswith(".main")
        finally:
            await client.close()
