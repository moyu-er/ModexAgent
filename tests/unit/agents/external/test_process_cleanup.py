"""Process cleanup regime tests.

Verifies the cleanup regime for the shared ``opencode serve`` subprocess owned
by ``OpenCodeServerManager`` (the process-global singleton):

- ``weakref.finalize`` on the manager fires on GC and kills the subprocess PID.
- ``_atexit_cleanup`` kills the live singleton's proc, skips dead procs, and
  skips when no singleton exists.
- ``_shutdown()`` detaches the finalizer (no double-kill on later GC).

The ``os_layer`` cleanup primitives (``register_signal_handlers`` idempotency,
signal-handler chaining, ``_sync_kill_proc`` platform branches) are also
covered here — ``os_layer`` is unchanged by the singleton refactor.
"""

from __future__ import annotations

import gc
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from modex_agent.agents.external import os_layer
from modex_agent.agents.external.providers.opencode import server_manager as opencode_server_manager
from modex_agent.agents.external.providers.opencode.server_manager import (
    OpenCodeServerManager,
    _atexit_cleanup,
)

_IS_WINDOWS = sys.platform == "win32"


def _make_env(modex_sid: str = "test_cleanup.opencode") -> dict[str, str]:
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


def _patch_manager_spawn_chain(
    monkeypatch: pytest.MonkeyPatch,
    process: Mock,
    kill_mock: Mock,
    tmp_path: Path,
) -> tuple[AsyncMock, AsyncMock]:
    """Patch spawn/readiness/SSE/client/kill used by ``OpenCodeServerManager.acquire``.

    Mocks live on the ``opencode_server_manager`` module (where the manager
    imports them), NOT on ``opencode_server_backend`` — the backend no longer
    owns the server lifecycle.
    """
    monkeypatch.setattr(opencode_server_manager, "_find_free_port", lambda: 43123)
    monkeypatch.setattr(
        opencode_server_manager,
        "resolve_executable",
        Mock(return_value=Mock(argv0="opencode", extra_args=())),
    )
    monkeypatch.setattr(
        opencode_server_manager,
        "spawn_process_group",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        opencode_server_manager,
        "terminate_process_group",
        AsyncMock(),
    )
    monkeypatch.setattr(opencode_server_manager, "_sync_kill_proc", kill_mock)
    monkeypatch.delenv("OPENCODE_HOST", raising=False)

    mock_reader = AsyncMock()
    mock_reader.start = AsyncMock()
    mock_reader.stop = AsyncMock()
    mock_reader.register_session = Mock()
    mock_reader.unregister_session = Mock()
    monkeypatch.setattr(
        opencode_server_manager, "OpenCodeV2SseReader", Mock(return_value=mock_reader)
    )

    mock_client = AsyncMock()
    mock_client.health = AsyncMock(return_value=True)
    mock_client.close = AsyncMock()
    monkeypatch.setattr(opencode_server_manager, "OpencodeV2Client", Mock(return_value=mock_client))

    # Isolate PID registry to tmp_path — _reap_orphaned_processes returns early
    # when the dir doesn't exist, and _register_pid/_unregister_pid operate on
    # tmp_path instead of ~/.config/modexagent/managed-opencode.
    monkeypatch.setattr(
        OpenCodeServerManager, "_registry_dir", lambda self: tmp_path / "pid_registry"
    )

    return mock_reader, mock_client


# ---------------------------------------------------------------------------
# weakref.finalize — fires on GC and kills subprocess PID
# ---------------------------------------------------------------------------


class TestWeakrefFinalizeKillsOnGC:
    async def test_gc_triggers_sync_kill(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        OpenCodeServerManager.reset_for_tests()
        process = _mock_process(pid=12345)
        kill_mock = Mock()
        _patch_manager_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        assert mgr._finalizer is not None

        # Clear the singleton + drop the handle (which holds mgr via _manager)
        # so the manager becomes unreferenced and GC fires the finalizer.
        OpenCodeServerManager.reset_for_tests()
        del handle
        del mgr
        gc.collect()

        kill_mock.assert_called_once_with(12345)


# ---------------------------------------------------------------------------
# _atexit_cleanup — kills the singleton's live proc
# ---------------------------------------------------------------------------


class TestAtexitCleanup:
    def test_atexit_kills_live_proc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        OpenCodeServerManager.reset_for_tests()
        kill_mock = Mock()
        monkeypatch.setattr(opencode_server_manager, "_sync_kill_proc", kill_mock)

        mgr = OpenCodeServerManager()
        mgr._proc = _mock_process(pid=11111)
        OpenCodeServerManager._instance = mgr

        try:
            _atexit_cleanup()
            kill_mock.assert_called_once_with(11111)
        finally:
            OpenCodeServerManager.reset_for_tests()

    def test_atexit_skips_already_dead_proc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        OpenCodeServerManager.reset_for_tests()
        kill_mock = Mock()
        monkeypatch.setattr(opencode_server_manager, "_sync_kill_proc", kill_mock)

        mgr = OpenCodeServerManager()
        proc = _mock_process(pid=33333)
        proc.returncode = 0
        mgr._proc = proc
        OpenCodeServerManager._instance = mgr

        try:
            _atexit_cleanup()
            kill_mock.assert_not_called()
        finally:
            OpenCodeServerManager.reset_for_tests()

    def test_atexit_skips_when_no_proc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        OpenCodeServerManager.reset_for_tests()
        kill_mock = Mock()
        monkeypatch.setattr(opencode_server_manager, "_sync_kill_proc", kill_mock)

        mgr = OpenCodeServerManager()
        mgr._proc = None
        OpenCodeServerManager._instance = mgr

        try:
            _atexit_cleanup()
            kill_mock.assert_not_called()
        finally:
            OpenCodeServerManager.reset_for_tests()

    def test_atexit_skips_when_no_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        OpenCodeServerManager.reset_for_tests()
        kill_mock = Mock()
        monkeypatch.setattr(opencode_server_manager, "_sync_kill_proc", kill_mock)

        _atexit_cleanup()
        kill_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _shutdown() — detaches finalizer (no double-kill on later GC)
# ---------------------------------------------------------------------------


class TestShutdownDetachesFinalizer:
    async def test_shutdown_detaches_and_no_double_kill(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        OpenCodeServerManager.reset_for_tests()
        process = _mock_process(pid=99999)
        kill_mock = Mock()
        _patch_manager_spawn_chain(monkeypatch, process, kill_mock, tmp_path)

        handle = await OpenCodeServerManager.acquire(tmp_path, _make_env())
        mgr = OpenCodeServerManager._instance
        assert mgr is not None
        assert mgr._finalizer is not None

        await mgr._shutdown()
        assert OpenCodeServerManager._instance is None

        del handle
        del mgr
        gc.collect()

        # _sync_kill_proc NOT called — finalizer was detached by _shutdown.
        kill_mock.assert_not_called()


# ---------------------------------------------------------------------------
# register_signal_handlers — idempotent
# ---------------------------------------------------------------------------


class TestRegisterSignalHandlersIdempotent:
    def test_called_twice_registers_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os_layer, "_signal_handlers_registered", False)
        signal_mock = Mock()
        monkeypatch.setattr(signal, "signal", signal_mock)
        monkeypatch.setattr(signal, "getsignal", lambda s: signal.SIG_DFL)

        os_layer.register_signal_handlers()
        os_layer.register_signal_handlers()

        # signal.signal called exactly twice (SIGTERM + SIGINT) on first call only.
        assert signal_mock.call_count == 2

    def test_first_call_registers_for_both_signals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os_layer, "_signal_handlers_registered", False)
        signal_mock = Mock()
        monkeypatch.setattr(signal, "signal", signal_mock)
        monkeypatch.setattr(signal, "getsignal", lambda s: signal.SIG_DFL)

        os_layer.register_signal_handlers()

        registered_sigs = {c.args[0] for c in signal_mock.call_args_list}
        assert registered_sigs == {signal.SIGTERM, signal.SIGINT}


# ---------------------------------------------------------------------------
# register_signal_handlers — runs atexit + sys.exit
# ---------------------------------------------------------------------------


class TestSignalHandlerRunsAtexit:
    def test_handler_calls_atexit_and_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os_layer, "_signal_handlers_registered", False)
        monkeypatch.setattr(signal, "getsignal", lambda s: signal.SIG_DFL)
        registered: dict[int, object] = {}

        def fake_signal(sig: int, handler: object) -> None:
            registered[sig] = handler

        monkeypatch.setattr(signal, "signal", fake_signal)

        atexit_run = Mock()
        sys_exit = Mock()
        monkeypatch.setattr("atexit._run_exitfuncs", atexit_run)
        monkeypatch.setattr("sys.exit", sys_exit)

        os_layer.register_signal_handlers()

        handler = registered[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)

        atexit_run.assert_called_once_with()
        sys_exit.assert_called_once_with(0)


# ---------------------------------------------------------------------------
# register_signal_handlers — chains to previous non-default handler
# ---------------------------------------------------------------------------


class TestRegisterSignalHandlersChaining:
    def test_chains_to_previous_non_default_handler(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(os_layer, "_signal_handlers_registered", False)
        prev_handler = Mock()

        def fake_getsignal(sig: int) -> object:
            if sig == signal.SIGTERM:
                return prev_handler
            return signal.SIG_DFL

        monkeypatch.setattr(signal, "getsignal", fake_getsignal)
        registered: dict[int, object] = {}

        def fake_signal(sig: int, handler: object) -> None:
            registered[sig] = handler

        monkeypatch.setattr(signal, "signal", fake_signal)
        monkeypatch.setattr("atexit._run_exitfuncs", Mock())
        monkeypatch.setattr("sys.exit", Mock())

        os_layer.register_signal_handlers()

        chained = registered[signal.SIGTERM]
        assert chained is not prev_handler  # wrapped
        assert callable(chained)

        # Trigger — prev handler should be called first (cooperative chaining).
        chained(signal.SIGTERM, None)
        prev_handler.assert_called_once_with(signal.SIGTERM, None)

    def test_does_not_chain_default_handler(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(os_layer, "_signal_handlers_registered", False)
        monkeypatch.setattr(signal, "getsignal", lambda s: signal.SIG_DFL)
        registered: dict[int, object] = {}

        def fake_signal(sig: int, handler: object) -> None:
            registered[sig] = handler

        monkeypatch.setattr(signal, "signal", fake_signal)
        atexit_run = Mock()
        monkeypatch.setattr("atexit._run_exitfuncs", atexit_run)
        monkeypatch.setattr("sys.exit", Mock())

        os_layer.register_signal_handlers()

        handler = registered[signal.SIGTERM]
        assert callable(handler)
        # Default path: handler runs atexit + exit directly, no chaining.
        handler(signal.SIGTERM, None)
        atexit_run.assert_called_once_with()


# ---------------------------------------------------------------------------
# _sync_kill_proc — platform branches
# ---------------------------------------------------------------------------


class TestSyncKillProc:
    @pytest.mark.skipif(_IS_WINDOWS, reason="POSIX branch")
    def test_posix_uses_os_kill_sigkill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        os_kill = Mock()
        monkeypatch.setattr("os.kill", os_kill)

        os_layer._sync_kill_proc(12345)

        os_kill.assert_called_once_with(12345, signal.SIGKILL)

    @pytest.mark.skipif(_IS_WINDOWS, reason="POSIX branch")
    def test_posix_suppresses_process_lookup_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.kill", Mock(side_effect=ProcessLookupError()))
        os_layer._sync_kill_proc(12345)  # must not raise

    @pytest.mark.skipif(_IS_WINDOWS, reason="POSIX branch")
    def test_posix_suppresses_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.kill", Mock(side_effect=OSError()))
        os_layer._sync_kill_proc(12345)  # must not raise

    @pytest.mark.skipif(not _IS_WINDOWS, reason="Windows branch")
    def test_windows_uses_taskkill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run_mock = Mock()
        monkeypatch.setattr(subprocess, "run", run_mock)

        os_layer._sync_kill_proc(12345)

        run_mock.assert_called_once_with(
            ["taskkill", "/F", "/T", "/PID", "12345"],
            capture_output=True,
            check=False,
        )

    @pytest.mark.skipif(not _IS_WINDOWS, reason="Windows branch")
    def test_windows_suppresses_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", Mock(side_effect=OSError()))
        os_layer._sync_kill_proc(12345)  # must not raise
