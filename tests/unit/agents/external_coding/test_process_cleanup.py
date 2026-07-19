"""Seam 5 — process cleanup regime tests.

Verifies the cleanup regime for ``opencode serve`` subprocesses:

- ``weakref.finalize`` fires on GC and kills the subprocess.
- ``atexit`` handler walks ``_live_server_backends`` and kills live procs.
- ``close()`` detaches the finalizer (no double-kill on later GC).
- ``register_signal_handlers()`` is idempotent (registered once).
- ``register_signal_handlers()`` runs atexit + sys.exit on signal.
- ``register_signal_handlers()`` chains to previous non-default handler.
- ``_sync_kill_proc`` uses ``os.kill(SIGKILL)`` on POSIX, ``taskkill`` on Windows.
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

from modex_agent.agents.external_coding import os_layer
from modex_agent.agents.external_coding.providers import opencode_server_backend
from modex_agent.agents.external_coding.providers.opencode_server_backend import (
    OpenCodeServerBackend,
    _atexit_cleanup,
    _live_server_backends,
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


def _patch_spawn_chain(
    monkeypatch: pytest.MonkeyPatch,
    process: Mock,
    kill_mock: Mock,
) -> None:
    """Patch the spawn/readiness/kill chain used by ``_ensure_server``."""
    monkeypatch.setattr(opencode_server_backend, "_sync_kill_proc", kill_mock)
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
        AsyncMock(),
    )
    monkeypatch.setattr(OpenCodeServerBackend, "_wait_ready", AsyncMock())


# ---------------------------------------------------------------------------
# weakref.finalize — fires on GC and kills subprocess
# ---------------------------------------------------------------------------


class TestWeakrefFinalizeKillsOnGC:
    async def test_gc_triggers_sync_kill(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Given: mocked spawn + immediate readiness + mocked sync kill
        process = _mock_process(pid=12345)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock)

        backend = OpenCodeServerBackend()
        await backend._ensure_server(tmp_path, _make_env())
        assert backend._finalizer is not None
        assert backend in _live_server_backends

        # When: drop the only strong reference — finalizer should fire on GC.
        del backend
        gc.collect()

        # Then: _sync_kill_proc called with the spawned PID.
        kill_mock.assert_called_once_with(12345)


# ---------------------------------------------------------------------------
# atexit — walks _live_server_backends and kills live procs
# ---------------------------------------------------------------------------


class TestAtexitCleanup:
    async def test_atexit_kills_all_live_backends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kill_mock = Mock()
        monkeypatch.setattr(opencode_server_backend, "_sync_kill_proc", kill_mock)

        b1 = OpenCodeServerBackend()
        b1._server_proc = _mock_process(pid=11111)
        b2 = OpenCodeServerBackend()
        b2._server_proc = _mock_process(pid=22222)
        _live_server_backends.add(b1)
        _live_server_backends.add(b2)

        try:
            _atexit_cleanup()
            killed_pids = {c.args[0] for c in kill_mock.call_args_list}
            assert killed_pids == {11111, 22222}
        finally:
            _live_server_backends.discard(b1)
            _live_server_backends.discard(b2)

    async def test_atexit_skips_already_dead_procs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kill_mock = Mock()
        monkeypatch.setattr(opencode_server_backend, "_sync_kill_proc", kill_mock)

        b = OpenCodeServerBackend()
        proc = _mock_process(pid=33333)
        proc.returncode = 0  # already exited
        b._server_proc = proc
        _live_server_backends.add(b)

        try:
            _atexit_cleanup()
            kill_mock.assert_not_called()
        finally:
            _live_server_backends.discard(b)

    async def test_atexit_skips_backends_with_no_proc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kill_mock = Mock()
        monkeypatch.setattr(opencode_server_backend, "_sync_kill_proc", kill_mock)

        b = OpenCodeServerBackend()
        _live_server_backends.add(b)

        try:
            _atexit_cleanup()
            kill_mock.assert_not_called()
        finally:
            _live_server_backends.discard(b)


# ---------------------------------------------------------------------------
# close() — detaches finalizer (no double-kill)
# ---------------------------------------------------------------------------


class TestCloseDetachesFinalizer:
    async def test_close_detaches_and_no_double_kill(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        process = _mock_process(pid=99999)
        kill_mock = Mock()
        _patch_spawn_chain(monkeypatch, process, kill_mock)

        backend = OpenCodeServerBackend()
        await backend._ensure_server(tmp_path, _make_env())
        assert backend._finalizer is not None
        assert backend in _live_server_backends

        # When: close() then GC.
        await backend.close()

        # Finalizer detached and backend removed from registry.
        assert backend._finalizer is None
        assert backend not in _live_server_backends

        del backend
        gc.collect()

        # Then: _sync_kill_proc NOT called — finalizer was detached.
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
