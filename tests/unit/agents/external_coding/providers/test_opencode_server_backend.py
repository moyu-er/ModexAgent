from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from modex_agent.agents.external_coding import Emission, ExternalCodingEvent
from modex_agent.agents.external_coding.providers import opencode_server_backend
from modex_agent.agents.external_coding.providers.opencode_server_backend import (
    OpenCodeServerBackend,
    SSEUnavailableError,
)
from modex_agent.agents.external_coding.types import BackendStatus, ExecOptions

_SKIP_REASON = "opencode CLI not installed or OPENCODE_SSE_INTEGRATION not set"


def _opencode_available() -> bool:
    return shutil.which("opencode") is not None and bool(
        os.environ.get("OPENCODE_SSE_INTEGRATION")
    )


def _make_env(modex_sid: str = "test_sse.opencode") -> dict[str, str]:
    """Build env matching ExternalEnvBuilder output (full os.environ + MODEX_*)."""
    env = dict(os.environ)
    env.update({
        "MODEX_SESSION_ID": modex_sid,
        "MODEX_AGENT_NAME": "opencode",
        "MODEX_INBOX_ROOT": os.environ.get("TEMP", "/tmp"),
        "MODEX_AGENT_POOL_MAP": "opencode=pool_opencode",
        "MODEX_TARGETS": "",
    })
    return env


@pytest.mark.asyncio
class TestOpenCodeServerStartupCleanup:
    async def test_close_waits_for_blocked_startup_and_terminates_server(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Given
        backend = OpenCodeServerBackend()
        process = Mock(returncode=None)
        process.wait = AsyncMock(return_value=0)
        readiness_started = asyncio.Event()
        release_readiness = asyncio.Event()
        terminate_process_tree = AsyncMock()

        async def blocked_readiness() -> None:
            readiness_started.set()
            await release_readiness.wait()

        monkeypatch.setattr(opencode_server_backend, "_find_free_port", lambda: 43123)
        monkeypatch.setattr(
            opencode_server_backend,
            "resolve_executable",
            Mock(return_value=Mock(argv0="opencode", extra_args=())),
        )
        monkeypatch.setattr(
            opencode_server_backend,
            "spawn_process_group",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(
            opencode_server_backend,
            "terminate_process_group",
            terminate_process_tree,
        )
        monkeypatch.setattr(backend, "_wait_ready", blocked_readiness)
        startup = asyncio.create_task(backend._ensure_server(tmp_path, _make_env()))
        await readiness_started.wait()

        # When
        closing = asyncio.create_task(backend.close())
        await asyncio.sleep(0)

        # Then
        assert not closing.done()
        release_readiness.set()
        await startup
        await closing
        terminate_process_tree.assert_awaited_once_with(process)

    async def test_startup_is_rejected_after_close(
        self,
        tmp_path: Path,
    ) -> None:
        # Given
        backend = OpenCodeServerBackend()
        await backend.close()

        # When / Then
        with pytest.raises(RuntimeError, match="closed"):
            await backend._ensure_server(tmp_path, _make_env())

    async def test_readiness_failure_rolls_back_spawned_server(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Given
        backend = OpenCodeServerBackend()
        process = Mock(returncode=None)
        process.wait = AsyncMock(return_value=0)
        readiness_error = SSEUnavailableError("readiness failed")
        wait_ready = AsyncMock(side_effect=readiness_error)
        terminate_process_tree = AsyncMock()
        monkeypatch.setattr(opencode_server_backend, "_find_free_port", lambda: 43123)
        monkeypatch.setattr(
            opencode_server_backend,
            "resolve_executable",
            Mock(return_value=Mock(argv0="opencode", extra_args=())),
        )
        monkeypatch.setattr(
            opencode_server_backend,
            "spawn_process_group",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(
            opencode_server_backend,
            "terminate_process_group",
            terminate_process_tree,
        )
        monkeypatch.setattr(backend, "_wait_ready", wait_ready)

        # When
        with pytest.raises(SSEUnavailableError) as raised:
            await backend._ensure_server(tmp_path, _make_env())

        # Then
        assert raised.value is readiness_error
        terminate_process_tree.assert_awaited_once_with(process)
        assert backend._server_proc is None
        assert backend._server_url is None
        assert backend._server_workdir is None
        assert backend._server_modex_sid is None

    async def test_cancellation_after_spawn_rolls_back_server(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Given
        backend = OpenCodeServerBackend()
        process = Mock(returncode=None)
        process.wait = AsyncMock(return_value=0)
        cancellation = asyncio.CancelledError("startup cancelled")
        wait_ready = AsyncMock(side_effect=cancellation)
        terminate_process_tree = AsyncMock()
        monkeypatch.setattr(opencode_server_backend, "_find_free_port", lambda: 43123)
        monkeypatch.setattr(
            opencode_server_backend,
            "resolve_executable",
            Mock(return_value=Mock(argv0="opencode", extra_args=())),
        )
        monkeypatch.setattr(
            opencode_server_backend,
            "spawn_process_group",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(
            opencode_server_backend,
            "terminate_process_group",
            terminate_process_tree,
        )
        monkeypatch.setattr(backend, "_wait_ready", wait_ready)

        # When
        with pytest.raises(asyncio.CancelledError) as raised:
            await backend._ensure_server(tmp_path, _make_env())

        # Then
        assert raised.value is cancellation
        terminate_process_tree.assert_awaited_once_with(process)
        assert backend._server_proc is None
        assert backend._server_url is None
        assert backend._server_workdir is None
        assert backend._server_modex_sid is None

    async def test_readiness_failure_remains_primary_when_termination_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Given
        backend = OpenCodeServerBackend()
        process = Mock(returncode=None)
        readiness_error = SSEUnavailableError("readiness failed")
        termination_error = ProcessLookupError("termination failed")
        monkeypatch.setattr(opencode_server_backend, "_find_free_port", lambda: 43123)
        monkeypatch.setattr(
            opencode_server_backend,
            "resolve_executable",
            Mock(return_value=Mock(argv0="opencode", extra_args=())),
        )
        monkeypatch.setattr(
            opencode_server_backend,
            "spawn_process_group",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(
            opencode_server_backend,
            "terminate_process_group",
            AsyncMock(side_effect=termination_error),
        )
        monkeypatch.setattr(
            backend,
            "_wait_ready",
            AsyncMock(side_effect=readiness_error),
        )

        # When
        with pytest.raises(SSEUnavailableError) as raised:
            await backend._ensure_server(tmp_path, _make_env())

        # Then
        assert raised.value is readiness_error
        assert backend._server_proc is process
        assert backend._server_url == "http://127.0.0.1:43123"
        assert backend._server_workdir == tmp_path

    async def test_cancellation_remains_cancellation_when_termination_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Given
        backend = OpenCodeServerBackend()
        process = Mock(returncode=None)
        cancellation = asyncio.CancelledError("startup cancelled")
        monkeypatch.setattr(opencode_server_backend, "_find_free_port", lambda: 43123)
        monkeypatch.setattr(
            opencode_server_backend,
            "resolve_executable",
            Mock(return_value=Mock(argv0="opencode", extra_args=())),
        )
        monkeypatch.setattr(
            opencode_server_backend,
            "spawn_process_group",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(
            opencode_server_backend,
            "terminate_process_group",
            AsyncMock(side_effect=ProcessLookupError("termination failed")),
        )
        monkeypatch.setattr(
            backend,
            "_wait_ready",
            AsyncMock(side_effect=cancellation),
        )

        # When
        with pytest.raises(asyncio.CancelledError) as raised:
            await backend._ensure_server(tmp_path, _make_env())

        # Then
        assert raised.value is cancellation
        assert backend._server_proc is process
        assert backend._server_url == "http://127.0.0.1:43123"
        assert backend._server_workdir == tmp_path

    async def test_forced_kill_awaits_final_exit_before_clearing_ownership(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Given
        backend = OpenCodeServerBackend()
        process = Mock(returncode=None)
        wait_calls = 0

        async def wait_for_exit() -> int:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                raise TimeoutError
            assert backend._server_proc is process
            assert backend._server_url == "http://127.0.0.1:43123"
            assert backend._server_workdir == tmp_path
            process.returncode = -9
            return -9

        process.wait = AsyncMock(side_effect=wait_for_exit)
        process.kill = Mock()
        backend._server_proc = process
        backend._server_url = "http://127.0.0.1:43123"
        backend._server_workdir = tmp_path
        backend._server_modex_sid = "session.opencode"
        terminate_process_tree = AsyncMock()
        monkeypatch.setattr(
            opencode_server_backend,
            "terminate_process_group",
            terminate_process_tree,
        )

        # When
        await backend._stop_server()

        # Then
        terminate_process_tree.assert_awaited_once_with(process)
        process.kill.assert_called_once_with()
        assert process.wait.await_count == 2
        assert backend._server_proc is None
        assert backend._server_url is None
        assert backend._server_workdir is None
        assert backend._server_modex_sid is None


@pytest.mark.skipif(not _opencode_available(), reason=_SKIP_REASON)
@pytest.mark.asyncio
class TestOpenCodeServerBackendIntegration:
    async def test_simple_prompt_streams_text_delta(self) -> None:
        backend = OpenCodeServerBackend()
        try:
            opts = ExecOptions(
                prompt="Say hello in exactly three words. Do not use any tools.",
                workdir=Path(os.environ.get("OPENCODE_TEST_WORKDIR", os.getcwd())),
            )
            env = _make_env("test_sse_1.opencode")
            emissions: list[Emission] = []

            async def on_emission(e: Emission) -> None:
                emissions.append(e)

            result = await backend.execute_streaming(opts, env, on_emission)

            assert result.status is BackendStatus.COMPLETED
            assert result.session_id is not None
            text_emissions = [e for e in emissions if e.event is ExternalCodingEvent.TEXT_DELTA]
            assert len(text_emissions) > 0
            combined = "".join(e.text or "" for e in text_emissions)
            assert len(combined) > 0
        finally:
            await backend.close()

    async def test_prompt_with_tool_yields_tool_use_and_result(self) -> None:
        backend = OpenCodeServerBackend()
        try:
            opts = ExecOptions(
                prompt="Read the first 5 lines of README.md, then summarize in one sentence.",
                workdir=Path(os.environ.get("OPENCODE_TEST_WORKDIR", os.getcwd())),
            )
            env = _make_env("test_sse_2.opencode")
            emissions: list[Emission] = []

            async def on_emission(e: Emission) -> None:
                emissions.append(e)

            result = await backend.execute_streaming(opts, env, on_emission)

            assert result.status is BackendStatus.COMPLETED
            tool_uses = [e for e in emissions if e.event is ExternalCodingEvent.TOOL_USE]
            assert len(tool_uses) >= 1
            tool_results = [e for e in emissions if e.event is ExternalCodingEvent.TOOL_RESULT]
            assert len(tool_results) >= 1
            text_emissions = [e for e in emissions if e.event is ExternalCodingEvent.TEXT_DELTA]
            assert len(text_emissions) > 0
        finally:
            await backend.close()

    async def test_session_resume_reuses_session_id(self) -> None:
        backend = OpenCodeServerBackend()
        try:
            workdir = Path(os.environ.get("OPENCODE_TEST_WORKDIR", os.getcwd()))
            env = _make_env("test_sse_3.opencode")

            opts1 = ExecOptions(prompt="Say hi.", workdir=workdir)
            emissions1: list[Emission] = []

            async def on_e1(e: Emission) -> None:
                emissions1.append(e)

            result1 = await backend.execute_streaming(opts1, env, on_e1)
            assert result1.status is BackendStatus.COMPLETED
            assert result1.session_id is not None

            opts2 = ExecOptions(
                prompt="Say bye.",
                workdir=workdir,
                resume_session_id=result1.session_id,
            )
            emissions2: list[Emission] = []

            async def on_e2(e: Emission) -> None:
                emissions2.append(e)

            result2 = await backend.execute_streaming(opts2, env, on_e2)
            assert result2.status is BackendStatus.COMPLETED
            assert result2.session_id == result1.session_id
        finally:
            await backend.close()
