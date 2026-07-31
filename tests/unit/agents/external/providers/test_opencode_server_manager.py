"""Unit tests for ``OpenCodeServerManager`` — the shared ``opencode serve`` singleton.

Tests the singleton lifecycle with mocked collaborators (process spawn, SSE
reader, HTTP client, PID registry) — never hits a real opencode binary or
network. Test isolation is via ``reset_for_tests()`` in an autouse fixture.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from modex_agent.agents.external.providers.opencode import server_manager as opencode_server_manager
from modex_agent.agents.external.providers.opencode.server_backend import (
    OpenCodeServerBackend,
)
from modex_agent.agents.external.providers.opencode.server_manager import (
    OpenCodeServerManager,
    _atexit_cleanup,
)

_PatchResult = tuple[AsyncMock, AsyncMock, list[AsyncMock], AsyncMock, AsyncMock]


def _make_env(modex_sid: str = "test_mgr.opencode") -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "MODEX_SESSION_ID": modex_sid,
            "MODEX_AGENT_NAME": "opencode",
            "MODEX_INBOX_ROOT": os.environ.get("TEMP", "/tmp"),
            "MODEX_AGENT_POOL_MAP": "opencode=pool_opencode",
            "MODEX_TARGETS": "",
        }
    )
    return env


def _mock_process(pid: int = 12345) -> Mock:
    proc = Mock()
    proc.pid = pid
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    proc.kill = Mock()
    return proc


def _make_reader_mock() -> AsyncMock:
    r = AsyncMock()
    r.start = AsyncMock()
    r.stop = AsyncMock()
    r.register_session = Mock()
    r.unregister_session = Mock()
    return r


def _patch_spawn_chain(
    monkeypatch: pytest.MonkeyPatch,
    process: Mock,
    kill_mock: Mock,
    tmp_path: Path,
) -> _PatchResult:
    """Patch spawn/readiness/SSE/client/kill on the manager module.

    Returns ``(spawn_mock, terminate_mock, readers, client_mock, health_mock)``
    where ``readers`` is a list that accumulates each SSE reader mock created
    during ``acquire()`` (one per workdir, one per respawn).
    """
    spawn_mock = AsyncMock(return_value=process)
    terminate_mock = AsyncMock()
    readers: list[AsyncMock] = []

    monkeypatch.setattr(opencode_server_manager, "_find_free_port", lambda: 43123)
    monkeypatch.setattr(
        opencode_server_manager,
        "resolve_executable",
        Mock(return_value=Mock(argv0="opencode", extra_args=())),
    )
    monkeypatch.setattr(opencode_server_manager, "spawn_process_group", spawn_mock)
    monkeypatch.setattr(opencode_server_manager, "terminate_process_group", terminate_mock)
    monkeypatch.setattr(opencode_server_manager, "_sync_kill_proc", kill_mock)
    monkeypatch.delenv("OPENCODE_HOST", raising=False)

    monkeypatch.setattr(
        opencode_server_manager,
        "OpenCodeV2SseReader",
        Mock(side_effect=lambda *a, **kw: readers.append(_make_reader_mock()) or readers[-1]),
    )

    health_mock = AsyncMock(return_value=True)
    client_mock = AsyncMock()
    client_mock.health = health_mock
    client_mock.close = AsyncMock()
    monkeypatch.setattr(
        opencode_server_manager,
        "OpencodeV2Client",
        Mock(return_value=client_mock),
    )

    monkeypatch.setattr(
        OpenCodeServerManager,
        "_registry_dir",
        lambda self: tmp_path / "pid_registry",
    )

    return spawn_mock, terminate_mock, readers, client_mock, health_mock


@pytest.fixture(autouse=True)
def _reset_manager() -> Iterator[None]:
    OpenCodeServerManager.reset_for_tests()
    yield
    OpenCodeServerManager.reset_for_tests()


# ---------------------------------------------------------------------------
# acquire — lazy spawn + reuse
# ---------------------------------------------------------------------------


class TestAcquireSpawnsAndReuses:
    async def test_first_acquire_spawns_process(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10001)
        kill_mock = Mock()
        spawn_mock, _, readers, client_mock, health_mock = _patch_spawn_chain(
            monkeypatch, process, kill_mock, tmp_path
        )

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())

        assert OpenCodeServerManager._instance is not None
        assert OpenCodeServerManager._instance._proc is process
        assert handle.server_url == "http://127.0.0.1:43123"
        assert handle.client is client_mock
        assert handle.parser is not None
        assert handle.sse_reader is readers[0]
        spawn_mock.assert_awaited_once()
        health_mock.assert_awaited()
        readers[0].start.assert_awaited_once()

    async def test_second_acquire_reuses_same_process(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10002)
        kill_mock = Mock()
        spawn_mock, _, readers, _, _ = _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        handle1 = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        spawn_count_after_first = spawn_mock.await_count

        handle2 = await OpenCodeServerManager.acquire(tmp_path, _make_env())

        assert handle2._manager is handle1._manager
        assert spawn_mock.await_count == spawn_count_after_first
        assert handle2.parser is handle1.parser
        assert handle2.sse_reader is handle1.sse_reader
        assert len(readers) == 1


# ---------------------------------------------------------------------------
# acquire — different workdir creates new SSE reader + parser
# ---------------------------------------------------------------------------


class TestAcquireDifferentWorkdir:
    async def test_different_workdir_creates_new_reader_and_parser(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10003)
        kill_mock = Mock()
        _, _, readers, _, _ = _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        workdir_a = tmp_path / "a"
        workdir_b = tmp_path / "b"
        workdir_a.mkdir()
        workdir_b.mkdir()

        handle_a = await OpenCodeServerManager.acquire(workdir_a, _make_env())
        handle_b = await OpenCodeServerManager.acquire(workdir_b, _make_env())

        assert handle_a._manager is handle_b._manager
        assert handle_a.parser is not handle_b.parser
        assert handle_a.sse_reader is not handle_b.sse_reader
        assert handle_a._workdir == str(workdir_a)
        assert handle_b._workdir == str(workdir_b)
        assert len(handle_a._manager._workdir_entries) == 2
        assert len(readers) == 2


# ---------------------------------------------------------------------------
# acquire — respawns if process died
# ---------------------------------------------------------------------------


class TestAcquireRespawnsOnDeadProcess:
    async def test_acquire_respawns_when_returncode_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        live_process_1 = _mock_process(pid=10004)
        live_process_2 = _mock_process(pid=10005)
        kill_mock = Mock()
        spawn_mock, _, _, _, _ = _patch_spawn_chain(
            monkeypatch, live_process_1, kill_mock, tmp_path
        )

        handle1 = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        assert handle1._manager._proc is live_process_1

        live_process_1.returncode = -1
        spawn_mock.return_value = live_process_2
        handle2 = await OpenCodeServerManager.acquire(tmp_path, _make_env())

        assert handle2._manager._proc is live_process_2
        assert spawn_mock.await_count == 2

    async def test_acquire_stops_old_readers_before_respawn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        live_process_1 = _mock_process(pid=10006)
        live_process_2 = _mock_process(pid=10007)
        kill_mock = Mock()
        spawn_mock, _, readers, _, _ = _patch_spawn_chain(
            monkeypatch, live_process_1, kill_mock, tmp_path
        )

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        assert readers[0].stop.await_count == 0

        live_process_1.returncode = 1
        spawn_mock.return_value = live_process_2
        await OpenCodeServerManager.acquire(tmp_path, _make_env())

        assert readers[0].stop.await_count >= 1

    async def test_acquire_respawn_clears_active_sessions_and_failures(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        live_process_1 = _mock_process(pid=10024)
        live_process_2 = _mock_process(pid=10025)
        kill_mock = Mock()
        spawn_mock, _, _, _, _ = _patch_spawn_chain(
            monkeypatch, live_process_1, kill_mock, tmp_path
        )

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = handle._manager
        handle.register_session("sess-stale-1")
        handle.register_session("sess-stale-2")
        mgr._consecutive_failures = 7
        mgr._first_failure_time = 12345.0
        assert len(mgr._active_sessions) == 2

        live_process_1.returncode = -1
        spawn_mock.return_value = live_process_2
        await OpenCodeServerManager.acquire(tmp_path, _make_env())

        assert len(mgr._active_sessions) == 0
        assert mgr._consecutive_failures == 0
        assert mgr._first_failure_time is None

        await mgr._shutdown()


# ---------------------------------------------------------------------------
# ServerHandle — register/unregister delegates to parser
# ---------------------------------------------------------------------------


class TestServerHandleRegisterUnregister:
    async def test_register_session_calls_parser_add_main_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10008)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mock_parser = Mock()
        handle.parser = mock_parser

        handle.register_session("sess-42")

        mock_parser.add_main_session.assert_called_once_with("sess-42")

    async def test_unregister_session_calls_parser_remove_main_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10009)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mock_parser = Mock()
        handle.parser = mock_parser

        handle.unregister_session("sess-99")

        mock_parser.remove_main_session.assert_called_once_with("sess-99")

    async def test_release_is_noop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10010)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        await handle.release()
        assert OpenCodeServerManager._instance is not None
        assert OpenCodeServerManager._instance._proc is process


# ---------------------------------------------------------------------------
# _shutdown — stops readers, closes client, terminates process, clears singleton
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_shutdown_stops_readers_and_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10011)
        kill_mock = Mock()
        _, terminate_mock, readers, client_mock, _ = _patch_spawn_chain(
            monkeypatch, process, kill_mock, tmp_path
        )

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None

        await mgr._shutdown()

        readers[0].stop.assert_awaited()
        client_mock.close.assert_awaited()
        terminate_mock.assert_awaited_once_with(process)
        assert OpenCodeServerManager._instance is None

    async def test_shutdown_skips_terminate_for_already_dead_process(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10012)
        kill_mock = Mock()
        _, terminate_mock, _, _, _ = _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        process.returncode = 0

        await mgr._shutdown()

        terminate_mock.assert_not_awaited()
        assert OpenCodeServerManager._instance is None

    async def test_shutdown_clears_lifecycle_bound_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10019)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        async with OpenCodeServerManager.lifecycle():
            await OpenCodeServerManager.acquire(tmp_path, _make_env())
            assert OpenCodeServerManager._lifecycle_bound is True

        assert OpenCodeServerManager._lifecycle_bound is False
        assert OpenCodeServerManager._instance is None

    async def test_shutdown_detaches_finalizer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10013)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        assert mgr._finalizer is not None

        await mgr._shutdown()

        assert mgr._finalizer is None

    async def test_shutdown_waits_for_active_sessions_to_clear(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(opencode_server_manager, "_SHUTDOWN_WAIT_ACTIVE", 0.3)
        process = _mock_process(pid=10020)
        kill_mock = Mock()
        _, terminate_mock, _, _, _ = _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        mgr._active_sessions.add("sess-wait-1")

        async def _clear_session_after_delay() -> None:
            await asyncio.sleep(0.05)
            mgr._active_sessions.discard("sess-wait-1")

        asyncio.create_task(_clear_session_after_delay())

        await mgr._shutdown()

        terminate_mock.assert_awaited_once_with(process)
        assert OpenCodeServerManager._instance is None

    async def test_shutdown_force_closes_after_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(opencode_server_manager, "_SHUTDOWN_WAIT_ACTIVE", 0.1)
        process = _mock_process(pid=10021)
        kill_mock = Mock()
        _, terminate_mock, _, _, _ = _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        mgr._active_sessions.add("sess-stuck-1")
        mgr._active_sessions.add("sess-stuck-2")

        with caplog.at_level(
            logging.WARNING,
            logger="modex_agent.agents.external.providers.opencode.server_manager",
        ):
            await mgr._shutdown()

        assert any("Force-closing" in r.message for r in caplog.records)
        terminate_mock.assert_awaited_once_with(process)
        assert OpenCodeServerManager._instance is None


# ---------------------------------------------------------------------------
# reset_for_tests — clears singleton
# ---------------------------------------------------------------------------


class TestResetForTests:
    async def test_reset_clears_instance_and_reaped_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10014)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        assert OpenCodeServerManager._instance is not None
        assert OpenCodeServerManager._reaped is True

        OpenCodeServerManager.reset_for_tests()

        assert OpenCodeServerManager._instance is None
        assert OpenCodeServerManager._reaped is False

    def test_reset_when_already_clear_is_noop(self) -> None:
        OpenCodeServerManager.reset_for_tests()
        OpenCodeServerManager.reset_for_tests()
        assert OpenCodeServerManager._instance is None
        assert OpenCodeServerManager._reaped is False

    def test_reset_clears_lifecycle_bound_flag(self) -> None:
        OpenCodeServerManager._lifecycle_bound = True
        OpenCodeServerManager.reset_for_tests()
        assert OpenCodeServerManager._lifecycle_bound is False


# ---------------------------------------------------------------------------
# lifecycle — async context manager binds singleton lifetime
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_lifecycle_enter_returns_manager(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=30001)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        async with OpenCodeServerManager.lifecycle() as mgr:
            assert mgr is OpenCodeServerManager._instance
            assert OpenCodeServerManager._lifecycle_bound is True

        assert OpenCodeServerManager._lifecycle_bound is False
        assert OpenCodeServerManager._instance is None

    async def test_lifecycle_exit_calls_shutdown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=30002)
        kill_mock = Mock()
        _, terminate_mock, readers, client_mock, _ = _patch_spawn_chain(
            monkeypatch, process, kill_mock, tmp_path
        )

        async with OpenCodeServerManager.lifecycle() as mgr:
            await OpenCodeServerManager.acquire(tmp_path, _make_env())

        readers[0].stop.assert_awaited()
        client_mock.close.assert_awaited()
        terminate_mock.assert_awaited_once_with(process)
        assert OpenCodeServerManager._instance is None
        assert mgr._shutting_down is True

    async def test_double_lifecycle_binding_raises_runtime_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=30003)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        async with OpenCodeServerManager.lifecycle():
            with pytest.raises(RuntimeError, match="lifecycle already bound"):
                OpenCodeServerManager.lifecycle()

        assert OpenCodeServerManager._lifecycle_bound is False

    async def test_lifecycle_creates_instance_if_none_exists(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=30004)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        assert OpenCodeServerManager._instance is None

        async with OpenCodeServerManager.lifecycle() as mgr:
            assert OpenCodeServerManager._instance is mgr

        assert OpenCodeServerManager._instance is None

    async def test_acquire_works_inside_lifecycle_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=30005)
        kill_mock = Mock()
        spawn_mock, _, readers, client_mock, _ = _patch_spawn_chain(
            monkeypatch, process, kill_mock, tmp_path
        )

        async with OpenCodeServerManager.lifecycle():
            handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
            assert handle.server_url == "http://127.0.0.1:43123"
            assert handle.client is client_mock
            spawn_mock.assert_awaited_once()
            readers[0].start.assert_awaited_once()

        assert OpenCodeServerManager._instance is None
        assert OpenCodeServerManager._lifecycle_bound is False

    async def test_lifecycle_can_rebind_after_previous_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=30006)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        async with OpenCodeServerManager.lifecycle():
            pass

        assert OpenCodeServerManager._lifecycle_bound is False

        async with OpenCodeServerManager.lifecycle() as mgr:
            assert mgr is OpenCodeServerManager._instance

        assert OpenCodeServerManager._lifecycle_bound is False


# ---------------------------------------------------------------------------
# weakref.finalize — fires on GC and kills process PID
# ---------------------------------------------------------------------------


class TestWeakrefFinalizeOnGC:
    async def test_gc_triggers_sync_kill(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10015)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None

        OpenCodeServerManager.reset_for_tests()
        del handle
        del mgr
        gc.collect()

        kill_mock.assert_called_once_with(10015)


# ---------------------------------------------------------------------------
# _atexit_cleanup — kills live process, skips dead process
# ---------------------------------------------------------------------------


class TestAtexitCleanup:
    def test_atexit_kills_live_singleton_proc(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        kill_mock = Mock()
        monkeypatch.setattr(opencode_server_manager, "_sync_kill_proc", kill_mock)

        mgr = OpenCodeServerManager()
        mgr._proc = _mock_process(pid=10016)
        OpenCodeServerManager._instance = mgr

        try:
            _atexit_cleanup()
            kill_mock.assert_called_once_with(10016)
        finally:
            OpenCodeServerManager.reset_for_tests()

    def test_atexit_skips_dead_singleton_proc(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        kill_mock = Mock()
        monkeypatch.setattr(opencode_server_manager, "_sync_kill_proc", kill_mock)

        mgr = OpenCodeServerManager()
        proc = _mock_process(pid=10017)
        proc.returncode = 0
        mgr._proc = proc
        OpenCodeServerManager._instance = mgr

        try:
            _atexit_cleanup()
            kill_mock.assert_not_called()
        finally:
            OpenCodeServerManager.reset_for_tests()

    def test_atexit_skips_when_no_singleton(self) -> None:
        OpenCodeServerManager.reset_for_tests()
        _atexit_cleanup()


# ---------------------------------------------------------------------------
# OPENCODE_HOST — external server skips spawn
# ---------------------------------------------------------------------------


class TestExternalServer:
    async def test_opencode_host_env_skips_spawn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10018)
        kill_mock = Mock()
        spawn_mock, _, _, _, health_mock = _patch_spawn_chain(
            monkeypatch, process, kill_mock, tmp_path
        )
        monkeypatch.setenv("OPENCODE_HOST", "http://external-host:9999")

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())

        spawn_mock.assert_not_awaited()
        assert handle.server_url == "http://external-host:9999"
        health_mock.assert_awaited()


# ---------------------------------------------------------------------------
# _spawn_server — neutralized cwd (process inherits bot cwd, not first workdir)
# ---------------------------------------------------------------------------


class TestSpawnServerNeutralizedCwd:
    async def test_spawn_uses_cwd_none_not_first_workdir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10020)
        kill_mock = Mock()
        spawn_mock, _, _, _, _ = _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)
        workdir = tmp_path / "project_a"
        workdir.mkdir()

        await OpenCodeServerManager.acquire(workdir, _make_env())

        spawn_mock.assert_awaited_once()
        assert spawn_mock.call_args.kwargs["cwd"] is None

    async def test_spawn_env_does_not_override_pwd_to_workdir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10021)
        kill_mock = Mock()
        spawn_mock, _, _, _, _ = _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)
        workdir = tmp_path / "project_b"
        workdir.mkdir()

        await OpenCodeServerManager.acquire(workdir, _make_env())

        spawn_env = spawn_mock.call_args.kwargs["env"]
        assert spawn_env.get("PWD") != str(workdir)


# ---------------------------------------------------------------------------
# per-session workdir passing — create_session_v1 receives the workdir string
# ---------------------------------------------------------------------------


class TestPerSessionWorkdirPassing:
    async def test_create_session_v1_receives_handle_workdir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=10022)
        kill_mock = Mock()
        _, _, _, client_mock, _ = _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)
        client_mock.create_session_v1 = AsyncMock(return_value="sess-v1-abc")
        workdir = tmp_path / "project_c"
        workdir.mkdir()

        handle = await OpenCodeServerManager.acquire(workdir, _make_env())

        assert handle._workdir == str(workdir)
        assert handle.client is client_mock

        # Simulate the backend's create_session_v1(str(opts.workdir)) call —
        # opts.workdir is the same value passed to acquire above.
        session_id = await handle.client.create_session_v1(handle._workdir)
        client_mock.create_session_v1.assert_awaited_once_with(str(workdir))
        assert session_id == "sess-v1-abc"


# ---------------------------------------------------------------------------
# Watchdog — starts after first spawn
# ---------------------------------------------------------------------------


class TestWatchdogStarts:
    async def test_watchdog_task_created_after_first_acquire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=20001)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        assert mgr._watchdog_task is not None
        assert not mgr._watchdog_task.done()

        await mgr._shutdown()

        assert mgr._watchdog_task is None


# ---------------------------------------------------------------------------
# Watchdog — detects dead process and respawns
# ---------------------------------------------------------------------------


class TestWatchdogDeadProcessRespawn:
    async def test_watchdog_respawns_when_returncode_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(opencode_server_manager, "_HEALTH_CHECK_INTERVAL", 0.01)

        live_proc_1 = _mock_process(pid=20002)
        live_proc_2 = _mock_process(pid=20003)
        kill_mock = Mock()
        spawn_mock, _, _, _, _ = _patch_spawn_chain(monkeypatch, live_proc_1, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        assert spawn_mock.await_count == 1

        spawn_mock.return_value = live_proc_2
        live_proc_1.returncode = -1

        await asyncio.sleep(0.15)

        assert spawn_mock.await_count == 2
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        assert mgr._proc is live_proc_2

        await mgr._shutdown()


# ---------------------------------------------------------------------------
# Watchdog — detects health failures and respawns after threshold
# ---------------------------------------------------------------------------


class TestWatchdogHealthFailureRespawn:
    async def test_watchdog_respawns_after_max_consecutive_failures(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(opencode_server_manager, "_HEALTH_CHECK_INTERVAL", 0.01)

        live_proc_1 = _mock_process(pid=20004)
        live_proc_2 = _mock_process(pid=20005)
        kill_mock = Mock()
        spawn_mock, _, _, _, health_mock = _patch_spawn_chain(
            monkeypatch, live_proc_1, kill_mock, tmp_path
        )
        health_mock.side_effect = [True] + [False] * 20 + [True] * 200

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        assert spawn_mock.await_count == 1

        spawn_mock.return_value = live_proc_2

        await asyncio.sleep(0.5)

        assert spawn_mock.await_count == 2
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        assert mgr._consecutive_failures == 0

        await mgr._shutdown()


# ---------------------------------------------------------------------------
# Watchdog — busy-session grace skips restart
# ---------------------------------------------------------------------------


class TestWatchdogBusySessionGrace:
    async def test_grace_skips_restart_when_sessions_active(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(opencode_server_manager, "_HEALTH_CHECK_INTERVAL", 0.01)

        process = _mock_process(pid=20006)
        kill_mock = Mock()
        spawn_mock, _, _, _, health_mock = _patch_spawn_chain(
            monkeypatch, process, kill_mock, tmp_path
        )
        health_mock.side_effect = [True] + [False] * 200

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        handle.register_session("sess-grace-1")

        assert spawn_mock.await_count == 1
        assert "sess-grace-1" in handle._manager._active_sessions

        await asyncio.sleep(0.5)

        assert spawn_mock.await_count == 1
        assert handle._manager._consecutive_failures >= 20
        assert "sess-grace-1" in handle._manager._active_sessions

        await handle._manager._shutdown()


# ---------------------------------------------------------------------------
# _respawn — clears old state and spawns new process
# ---------------------------------------------------------------------------


class TestRespawnClearsState:
    async def test_respawn_stops_readers_clears_entries_and_spawns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        live_proc_1 = _mock_process(pid=20007)
        live_proc_2 = _mock_process(pid=20008)
        kill_mock = Mock()
        spawn_mock, _, readers, _, _ = _patch_spawn_chain(
            monkeypatch, live_proc_1, kill_mock, tmp_path
        )

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        assert len(mgr._workdir_entries) == 1

        spawn_mock.return_value = live_proc_2
        await mgr._respawn()

        assert spawn_mock.await_count == 2
        assert len(mgr._workdir_entries) == 0
        assert len(mgr._active_sessions) == 0
        assert mgr._consecutive_failures == 0
        assert mgr._first_failure_time is None
        assert readers[0].stop.await_count >= 1

        await mgr._shutdown()


# ---------------------------------------------------------------------------
# _respawn — resets consecutive_failures
# ---------------------------------------------------------------------------


class TestRespawnResetsFailures:
    async def test_respawn_resets_consecutive_failures_and_first_failure_time(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        live_proc_1 = _mock_process(pid=20009)
        live_proc_2 = _mock_process(pid=20010)
        kill_mock = Mock()
        spawn_mock, _, _, _, _ = _patch_spawn_chain(monkeypatch, live_proc_1, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None

        mgr._consecutive_failures = 5
        mgr._first_failure_time = 12345.0

        spawn_mock.return_value = live_proc_2
        await mgr._respawn()

        assert mgr._consecutive_failures == 0
        assert mgr._first_failure_time is None

        await mgr._shutdown()


# ---------------------------------------------------------------------------
# _shutdown — stops watchdog
# ---------------------------------------------------------------------------


class TestShutdownStopsWatchdog:
    async def test_shutdown_cancels_watchdog_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=20011)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        assert mgr._watchdog_task is not None

        await mgr._shutdown()

        assert mgr._watchdog_task is None
        assert mgr._shutting_down is True


# ---------------------------------------------------------------------------
# ServerHandle — register/unregister tracks _active_sessions
# ---------------------------------------------------------------------------


class TestServerHandleActiveSessionsTracking:
    async def test_register_session_adds_to_active_sessions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=20012)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        assert len(handle._manager._active_sessions) == 0

        handle.register_session("sess-active")

        assert "sess-active" in handle._manager._active_sessions

        await handle._manager._shutdown()

    async def test_unregister_session_removes_from_active_sessions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=20013)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        handle.register_session("sess-active")
        assert "sess-active" in handle._manager._active_sessions

        handle.unregister_session("sess-active")

        assert "sess-active" not in handle._manager._active_sessions

        await handle._manager._shutdown()


# ---------------------------------------------------------------------------
# Poll-phase dead-process detection — backend raises RuntimeError
# ---------------------------------------------------------------------------


class TestPollStatusDeadProcessDetection:
    async def test_poll_raises_runtime_error_when_proc_already_dead(self) -> None:
        mgr = OpenCodeServerManager()
        proc = Mock()
        proc.returncode = -1
        mgr._proc = proc
        OpenCodeServerManager._instance = mgr

        try:
            backend = OpenCodeServerBackend()
            mock_client = AsyncMock()
            mock_client.get_session_status_v1 = AsyncMock(return_value="busy")
            mock_handle = Mock()
            mock_handle.client = mock_client
            backend._handle = mock_handle

            with pytest.raises(RuntimeError, match="opencode process died"):
                await backend._poll_status_v1("test-sess-dead", directory="/tmp/test")
        finally:
            OpenCodeServerManager.reset_for_tests()

    async def test_poll_raises_runtime_error_on_conn_error_with_dead_proc(self) -> None:
        mgr = OpenCodeServerManager()
        proc = Mock()
        proc.returncode = None
        mgr._proc = proc
        OpenCodeServerManager._instance = mgr

        try:
            backend = OpenCodeServerBackend()
            mock_client = AsyncMock()
            call_count = 0

            def status_side_effect(session_id: str, *, directory: str | None = None) -> str:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return "busy"
                proc.returncode = -1
                raise OSError("connection refused")

            mock_client.get_session_status_v1 = AsyncMock(side_effect=status_side_effect)
            mock_handle = Mock()
            mock_handle.client = mock_client
            backend._handle = mock_handle

            with pytest.raises(RuntimeError, match="opencode process died"):
                await backend._poll_status_v1("test-sess-conn", directory="/tmp/test")
        finally:
            OpenCodeServerManager.reset_for_tests()


# ---------------------------------------------------------------------------
# is_process_dead — classmethod predicate
# ---------------------------------------------------------------------------


class TestIsProcessDead:
    async def test_returns_false_when_no_singleton(self) -> None:
        OpenCodeServerManager.reset_for_tests()
        assert OpenCodeServerManager.is_process_dead() is False

    async def test_returns_false_when_no_proc(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=20014)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        mgr._proc = None

        assert OpenCodeServerManager.is_process_dead() is False

        await mgr._shutdown()

    async def test_returns_false_when_proc_alive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=20015)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None

        assert OpenCodeServerManager.is_process_dead() is False

        await mgr._shutdown()

    async def test_returns_true_when_returncode_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=20016)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        process.returncode = -1

        assert OpenCodeServerManager.is_process_dead() is True

        await mgr._shutdown()
