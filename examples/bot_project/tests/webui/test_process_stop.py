"""Tests for process discovery and stop/kill logic.

Uses monkeypatch on subprocess to avoid actually killing processes.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Never
from unittest.mock import MagicMock

import pytest

# ── Import the module under test ────────────────────────────────────────────


@pytest.fixture
def proc_module():
    """Import modexbot.cli as a module for testing its helpers."""

    import modexbot.cli as cli_mod

    return cli_mod


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_cp(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    *,
    text: bool = False,
) -> MagicMock:
    """Create a CompletedProcess mock — stdout/stderr are bytes (decoded when text=True)."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout.decode("utf-8", errors="replace") if text else stdout
    cp.stderr = stderr.decode("utf-8", errors="replace") if text else stderr
    return cp


# ── Test: port-based discovery (Windows) ────────────────────────────────────


def test_find_processes_by_port_windows(proc_module, monkeypatch) -> None:
    """netstat -ano → extract PIDs listening on port."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")

    def mock_run(args, **kwargs):
        text = kwargs.get("text", False)
        if isinstance(args, list) and args[0] == "netstat":
            return _make_cp(
                returncode=0,
                stdout=(
                    b"  TCP    0.0.0.0:21800           0.0.0.0:0              LISTENING       12345\r\n"
                    b"  TCP    0.0.0.0:21800           0.0.0.0:0              LISTENING       12346\r\n"
                ),
                text=text,
            )
        return _make_cp(returncode=1, stdout=b"", text=text)

    monkeypatch.setattr(subprocess, "run", mock_run)
    pids = proc_module._find_processes_by_port(21800)
    assert 12345 in pids
    assert 12346 in pids
    assert len(pids) == 2  # deduplicated


def test_find_processes_by_port_empty(proc_module, monkeypatch) -> None:
    """No process on port → empty list."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")

    def mock_run(args, **kwargs):
        return _make_cp(returncode=1, stdout=b"", text=kwargs.get("text", False))

    monkeypatch.setattr(subprocess, "run", mock_run)
    pids = proc_module._find_processes_by_port(21800)
    assert pids == []


# ── Test: command-line discovery ────────────────────────────────────────────


def test_find_processes_by_command_windows(proc_module, monkeypatch) -> None:
    """Powershell Get-CimInstance → extract PIDs."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")

    def mock_run(args, **kwargs):
        text = kwargs.get("text", False)
        if isinstance(args, list) and "powershell" in str(args).lower():
            return _make_cp(
                returncode=0,
                stdout=b"12345\r\n12346\r\n",
                text=text,
            )
        return _make_cp(returncode=1, stdout=b"", text=text)

    monkeypatch.setattr(subprocess, "run", mock_run)
    pids = proc_module._find_processes_by_command("modexbot.cli")
    assert len(pids) >= 2
    assert 12345 in pids
    assert 12346 in pids


# ── Test: is_port_in_use ────────────────────────────────────────────────────


def test_is_port_in_use_true(proc_module, monkeypatch) -> None:
    """Port 21800 is in use → returns True."""
    monkeypatch.setattr(
        proc_module, "_find_processes_by_port", lambda port: [12345]
    )
    assert proc_module._is_port_in_use(21800) is True


def test_is_port_in_use_false(proc_module, monkeypatch) -> None:
    """Port 21800 is free → returns False."""
    monkeypatch.setattr(
        proc_module, "_find_processes_by_port", lambda port: []
    )
    assert proc_module._is_port_in_use(21800) is False


# ── Test: bot process validation ────────────────────────────────────────────


def test_get_command_line_windows(proc_module, monkeypatch) -> None:
    """Windows: PowerShell returns the command line for the PID."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")

    def mock_run(args, **kwargs):
        return _make_cp(
            stdout=b"python -m modexbot.cli start\n",
            text=kwargs.get("text", False),
        )

    monkeypatch.setattr(subprocess, "run", mock_run)
    assert proc_module._get_command_line(12345) == "python -m modexbot.cli start"


def test_get_command_line_unreadable(proc_module, monkeypatch) -> None:
    """Subprocess fails → return None."""

    def mock_run(args, **kwargs) -> Never:
        raise OSError("permission denied")

    monkeypatch.setattr(subprocess, "run", mock_run)
    assert proc_module._get_command_line(12345) is None


def test_is_bot_process_true(proc_module, monkeypatch) -> None:
    """Command line contains a bot marker."""
    monkeypatch.setattr(
        proc_module,
        "_get_command_line",
        lambda pid: "python -m modexbot.cli start",
    )
    assert proc_module._is_bot_process(12345) is True


def test_is_bot_process_false(proc_module, monkeypatch) -> None:
    """Command line has no bot marker."""
    monkeypatch.setattr(
        proc_module, "_get_command_line", lambda pid: "nginx worker"
    )
    assert proc_module._is_bot_process(12345) is False


def test_is_bot_process_unreadable(proc_module, monkeypatch) -> None:
    """Command line cannot be read → not treated as bot."""
    monkeypatch.setattr(proc_module, "_get_command_line", lambda pid: None)
    assert proc_module._is_bot_process(12345) is False


# ── Test: kill by PID ───────────────────────────────────────────────────────


def test_kill_process_windows(proc_module, monkeypatch) -> None:
    """taskkill /pid {pid} /f → kills process."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")

    calls: list[list[str]] = []

    def mock_run(args, **kwargs):
        calls.append(list(args) if isinstance(args, list) else [str(args)])
        return _make_cp(returncode=0, stdout=b"SUCCESS", text=kwargs.get("text", False))

    monkeypatch.setattr(subprocess, "run", mock_run)
    result = proc_module._kill_process(12345)
    assert result is True
    assert any("12345" in " ".join(c) for c in calls)


def test_kill_process_failure(proc_module, monkeypatch) -> None:
    """Kill fails → returns False."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")

    def mock_run(args, **kwargs) -> Never:
        raise OSError("permission denied")

    monkeypatch.setattr(subprocess, "run", mock_run)
    result = proc_module._kill_process(12345)
    assert result is False


# ── Test: _stop_running orchestration ───────────────────────────────────────


def _patch_all_bot(monkeypatch, proc_module) -> None:
    """Treat every discovered PID as a modexbot process."""
    monkeypatch.setattr(proc_module, "_is_bot_process", lambda pid, via_port=False: True)


def test_stop_running_by_pid_file(proc_module, monkeypatch, tmp_path) -> None:
    """PID file exists, process is alive and is a bot → kill by PID."""
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("12345")

    monkeypatch.setattr(proc_module, "_PID_FILE", pid_file)
    _patch_all_bot(monkeypatch, proc_module)
    monkeypatch.setattr(proc_module, "_find_processes_by_port", lambda port: [])

    alive = [True]
    monkeypatch.setattr(proc_module, "_is_running", lambda pid: alive[0])

    killed: list[int] = []

    def mock_kill(pid) -> bool:
        killed.append(pid)
        alive[0] = False
        return True

    monkeypatch.setattr(proc_module, "_kill_process", mock_kill)

    result = proc_module._stop_running(port=21800)
    assert result is True
    assert 12345 in killed
    assert not pid_file.exists()


def test_stop_running_ignores_non_bot_pid_file(
    proc_module, monkeypatch, tmp_path
) -> None:
    """PID file points to a non-bot process → do not kill, remove stale file."""
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("12345")

    monkeypatch.setattr(proc_module, "_PID_FILE", pid_file)
    monkeypatch.setattr(proc_module, "_is_running", lambda pid: True)
    monkeypatch.setattr(proc_module, "_is_bot_process", lambda pid, via_port=False: False)
    monkeypatch.setattr(proc_module, "_find_processes_by_port", lambda port: [])

    killed: list[int] = []
    monkeypatch.setattr(proc_module, "_kill_process", lambda pid: killed.append(pid) or True)

    result = proc_module._stop_running(port=21800)
    assert result is True
    assert 12345 not in killed
    assert not pid_file.exists()


def test_stop_running_stale_pid_falls_back_to_port(
    proc_module, monkeypatch, tmp_path
) -> None:
    """PID file is stale → fall back to port scan and kill validated bot."""
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("99999")

    monkeypatch.setattr(proc_module, "_PID_FILE", pid_file)
    _patch_all_bot(monkeypatch, proc_module)
    monkeypatch.setattr(
        proc_module, "_find_processes_by_port", lambda port: [12345]
    )

    alive = {99999: False, 12345: True}

    def is_running(pid):
        return alive.get(pid, False)

    monkeypatch.setattr(proc_module, "_is_running", is_running)

    killed: list[int] = []

    def mock_kill(pid) -> bool:
        killed.append(pid)
        alive[pid] = False
        return True

    monkeypatch.setattr(proc_module, "_kill_process", mock_kill)

    result = proc_module._stop_running(port=21800)
    assert result is True
    assert 99999 not in killed
    assert 12345 in killed
    assert not pid_file.exists()


def test_stop_running_no_pid_file_falls_back_to_port(
    proc_module, monkeypatch
) -> None:
    """No PID file → scan port and kill validated bot."""
    monkeypatch.setattr(proc_module, "_read_pid", lambda: None)
    _patch_all_bot(monkeypatch, proc_module)
    monkeypatch.setattr(
        proc_module, "_find_processes_by_port", lambda port: [12345]
    )

    alive = [True]
    monkeypatch.setattr(proc_module, "_is_running", lambda pid: alive[0])

    killed: list[int] = []

    def mock_kill(pid) -> bool:
        killed.append(pid)
        alive[0] = False
        return True

    monkeypatch.setattr(proc_module, "_kill_process", mock_kill)

    result = proc_module._stop_running(port=21800)
    assert result is True
    assert 12345 in killed


def test_stop_running_port_ignores_non_bot(proc_module, monkeypatch) -> None:
    """Port is used by a non-bot process → do not kill it."""
    monkeypatch.setattr(proc_module, "_read_pid", lambda: None)
    monkeypatch.setattr(proc_module, "_is_bot_process", lambda pid, via_port=False: False)
    monkeypatch.setattr(
        proc_module, "_find_processes_by_port", lambda port: [12345]
    )
    monkeypatch.setattr(proc_module, "_is_running", lambda pid: True)

    killed: list[int] = []
    monkeypatch.setattr(
        proc_module, "_kill_process", lambda pid: killed.append(pid) or True
    )

    result = proc_module._stop_running(port=21800)
    assert result is True
    assert 12345 not in killed


def test_stop_running_excludes_current_pid(proc_module, monkeypatch) -> None:
    """The current process is never killed, even if it owns the port."""
    monkeypatch.setattr(proc_module, "_read_pid", lambda: None)
    _patch_all_bot(monkeypatch, proc_module)
    monkeypatch.setattr(
        proc_module, "_find_processes_by_port", lambda port: [12345]
    )
    monkeypatch.setattr(proc_module, "_is_running", lambda pid: True)
    monkeypatch.setattr("os.getpid", lambda: 12345)

    killed: list[int] = []
    monkeypatch.setattr(
        proc_module, "_kill_process", lambda pid: killed.append(pid) or True
    )

    result = proc_module._stop_running(port=21800)
    assert result is True
    assert 12345 not in killed


def test_stop_running_nothing_found(proc_module, monkeypatch) -> None:
    """No PID file and no port occupant → nothing to stop."""
    monkeypatch.setattr(proc_module, "_read_pid", lambda: None)
    monkeypatch.setattr(proc_module, "_find_processes_by_port", lambda port: [])

    result = proc_module._stop_running(port=21800)
    assert result is True


def test_stop_running_force(proc_module, monkeypatch) -> None:
    """Multiple bot PIDs on port → kill ALL of them."""
    monkeypatch.setattr(proc_module, "_read_pid", lambda: None)
    _patch_all_bot(monkeypatch, proc_module)
    monkeypatch.setattr(
        proc_module,
        "_find_processes_by_port",
        lambda port: [12345, 12346, 12347],
    )

    alive = {12345: True, 12346: True, 12347: True}

    def is_running(pid):
        return alive.get(pid, False)

    monkeypatch.setattr(proc_module, "_is_running", is_running)

    killed: list[int] = []

    def mock_kill(pid) -> bool:
        killed.append(pid)
        alive[pid] = False
        return True

    monkeypatch.setattr(proc_module, "_kill_process", mock_kill)

    result = proc_module._stop_running(port=21800)
    assert result is True
    assert sorted(killed) == [12345, 12346, 12347]
