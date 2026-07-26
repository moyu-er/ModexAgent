"""Tests for POST /api/workspace/pick (combined picker + workspace switch).

The picker runs ``tkinter.filedialog.askdirectory`` in a subprocess managed
by ``asyncio.create_subprocess_exec``. These tests mock the subprocess
creation so they run on any OS without a display. The endpoint combines
folder selection and workspace switching into one request.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.webui.server import WebUIServer
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.models import CdResult
from modex_agent.workspace.port import WorkspaceControlPort


class _FakeControl(WorkspaceControlPort):
    def __init__(self, home: Path, *, succeed: bool = True, notice: str = "") -> None:
        self._home = home
        self._succeed = succeed
        self._notice = notice
        self.opened: list[str] = []

    def current(self, session_id: str) -> Path:
        return self._home

    @property
    def home(self) -> Path:
        return self._home

    def pwd(self, session_id: str) -> str:
        return str(self._home)

    async def open_workspace(self, target: str):
        self.opened.append(target)
        return CdResult(
            success=self._succeed,
            current_path=Path(target),
            original_path=self._home,
            notice=self._notice,
        )

    async def switch(self, session_id: str, target: str):
        return CdResult(success=True, current_path=Path(target), original_path=self._home, notice="")

    async def exit(self, session_id: str):
        return await self.switch(session_id, str(self._home))


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode


def _make_server(tmp: str, control: _FakeControl | None = None) -> WebUIServer:
    workspace_root = Path(tmp)
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
    server = WebUIServer(
        input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    if control is not None:
        server.set_workspace_control(control)
    return server


@pytest.mark.asyncio
async def test_pick_returns_success_and_cwd_when_user_picks() -> None:
    picked_path = str(Path("/home/user/project"))
    with tempfile.TemporaryDirectory() as tmp:
        control = _FakeControl(Path(tmp), succeed=True)
        server = _make_server(tmp, control)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc(picked_path.encode())),
            ):
                resp = await client.post("/api/workspace/pick", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            assert data["path"] == picked_path
            assert data["cwd"] == picked_path
            assert control.opened == [picked_path]
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pick_returns_null_when_user_cancels() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        control = _FakeControl(Path(tmp))
        server = _make_server(tmp, control)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc(b"")),
            ):
                resp = await client.post("/api/workspace/pick", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["path"] is None
            assert data["success"] is False
            assert control.opened == []
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pick_returns_503_when_subprocess_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        server = _make_server(tmp)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc(b"", b"no display", returncode=1)),
            ):
                resp = await client.post("/api/workspace/pick", json={})
            assert resp.status == 503
            data = await resp.json()
            assert "error" in data
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pick_returns_503_on_other_exception() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        server = _make_server(tmp)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=RuntimeError("unexpected")),
            ):
                resp = await client.post("/api/workspace/pick", json={})
            assert resp.status == 503
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pick_returns_503_on_create_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        server = _make_server(tmp)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=OSError("no python")),
            ):
                resp = await client.post("/api/workspace/pick", json={})
            assert resp.status == 503
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pick_returns_failure_when_open_workspace_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        control = _FakeControl(Path(tmp), succeed=False, notice="Permission denied")
        server = _make_server(tmp, control)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc(b"/bad/path")),
            ):
                resp = await client.post("/api/workspace/pick", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is False
            assert data["notice"] == "Permission denied"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pick_handles_no_body() -> None:
    picked_path = str(Path("/picked"))
    with tempfile.TemporaryDirectory() as tmp:
        control = _FakeControl(Path(tmp))
        server = _make_server(tmp, control)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc(picked_path.encode())),
            ):
                resp = await client.post("/api/workspace/pick")
            assert resp.status == 200
            data = await resp.json()
            assert data["path"] == picked_path
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pick_returns_504_on_timeout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        server = _make_server(tmp)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            fake_proc = _FakeProc(b"")
            fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=fake_proc),
            ):
                resp = await client.post("/api/workspace/pick", json={})
            assert resp.status == 504
            data = await resp.json()
            assert "timed out" in data["error"].lower()
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pick_returns_failure_when_no_workspace_control() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        server = _make_server(tmp, control=None)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc(b"/selected")),
            ):
                resp = await client.post("/api/workspace/pick", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is False
            assert "not configured" in data["notice"].lower()
        finally:
            await client.close()
