#!/usr/bin/env python3
"""Bot control script — cross-platform start / stop / restart.

Usage:
    python botctl.py [stop|restart] [--help|-help|--h|-h]

Commands:
    stop     Stop all bot_service.py processes.
    restart  Start a new instance, then stop all old ones (default).
"""

from __future__ import annotations

import argparse
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

BOT_SERVICE_NAME = "bot_service.py"
PID_FILE_NAME = ".bot.pid"
LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "bot.log"
BOT_MARKER = "bot_service.py"


# ── helpers ──────────────────────────────────────────────────────────────


def _bot_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _pid_file() -> Path:
    return _bot_dir() / PID_FILE_NAME


def _read_pid() -> int | None:
    pf = _pid_file()
    if not pf.exists():
        return None
    try:
        return int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        pf.unlink(missing_ok=True)
        return None


def _write_pid(pid: int) -> None:
    _pid_file().write_text(str(pid), encoding="utf-8")


def _remove_pid() -> None:
    _pid_file().unlink(missing_ok=True)


def _is_running(pid: int) -> bool:
    system = platform.system()
    if system == "Windows":
        result = subprocess.run(
            ["tasklist", "/fi", f"pid eq {pid}", "/fo", "csv", "/nh"],
            capture_output=True,
        )
        return f'"{pid}"' in result.stdout.decode("utf-8", errors="replace")
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ── process discovery (cmdline-based, strict) ────────────────────────────


def _scan_bot_cmdlines() -> set[int]:
    """Return raw PIDs from cmdline scan (may include recently-exited)."""
    system = platform.system()
    pids: set[int] = set()

    if system == "Windows":
        ps_cmd = (
            "Get-CimInstance Win32_Process |"
            " Where-Object { $_.CommandLine -like '*bot_service.py*' } |"
            " ForEach-Object { $_.ProcessId }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
        )
        if result.returncode == 0:
            for token in result.stdout.decode("utf-8", errors="replace").split():
                try:
                    pids.add(int(token.strip()))
                except ValueError:
                    pass

        if not pids:
            result = subprocess.run(
                ["wmic", "process", "get", "processid,commandline", "/format:csv"],
                capture_output=True,
            )
            for line in result.stdout.decode("utf-8", errors="replace").splitlines():
                if BOT_MARKER in line:
                    parts = [c.strip() for c in line.split(",")]
                    if len(parts) >= 2:
                        try:
                            pids.add(int(parts[-2]))
                        except ValueError:
                            pass
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-f", r"bot_service\.py"],
                capture_output=True,
            )
            for token in result.stdout.decode().split():
                try:
                    pids.add(int(token.strip()))
                except ValueError:
                    pass
        except FileNotFoundError:
            result = subprocess.run(["ps", "aux"], capture_output=True)
            for line in result.stdout.decode("utf-8", errors="replace").splitlines():
                if BOT_MARKER in line and "grep" not in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            pids.add(int(parts[1]))
                        except ValueError:
                            pass

    pids.discard(os.getpid())
    return pids


def _find_bot_pids() -> set[int]:
    """Return PIDs of **running** processes whose cmdline contains bot_service.py."""
    return {p for p in _scan_bot_cmdlines() if _is_running(p)}


# ── termination ──────────────────────────────────────────────────────────


def _terminate_root(pid: int, grace_period: int = 5) -> bool:
    """Kill *pid* only (no tree walk).  Safe for self-restart scenarios."""
    system = platform.system()

    if system == "Windows":
        if hasattr(signal, "CTRL_BREAK_EVENT"):
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            except (OSError, ProcessLookupError):
                pass
        for _ in range(grace_period):
            if not _is_running(pid):
                return True
            time.sleep(1)
        subprocess.run(["taskkill", "/pid", str(pid), "/f"], capture_output=True)
        time.sleep(1)
        return not _is_running(pid)

    # Unix
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    for _ in range(grace_period):
        if not _is_running(pid):
            return True
        time.sleep(1)
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except (OSError, ProcessLookupError):
        pass
    time.sleep(1)
    return not _is_running(pid)


def _terminate_tree(pid: int, grace_period: int = 15) -> bool:
    """Kill *pid* and its entire process tree.  Used by stop() for deep cleanup."""
    system = platform.system()

    if system == "Windows":
        if hasattr(signal, "CTRL_BREAK_EVENT"):
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            except (OSError, ProcessLookupError):
                pass
        for _ in range(grace_period):
            if not _is_running(pid):
                return True
            time.sleep(1)
        subprocess.run(
            ["taskkill", "/pid", str(pid), "/t", "/f"],
            capture_output=True,
        )
        time.sleep(1)
        return not _is_running(pid)

    # Unix — killpg so children (uv → python) go down together.
    if hasattr(os, "getpgid") and hasattr(os, "killpg"):
        try:
            pgid = os.getpgid(pid)  # type: ignore[attr-defined]
            os.killpg(pgid, signal.SIGTERM)  # type: ignore[attr-defined]
        except (OSError, ProcessLookupError):
            pass
        for _ in range(grace_period):
            if not _is_running(pid):
                return True
            time.sleep(1)
        try:
            pgid = os.getpgid(pid)  # type: ignore[attr-defined]
            os.killpg(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))  # type: ignore[attr-defined]
        except (OSError, ProcessLookupError):
            pass
        time.sleep(1)
    return not _is_running(pid)


# ── public commands ──────────────────────────────────────────────────────


def stop() -> bool:
    """Stop every bot_service.py process on the system."""
    pids = _find_bot_pids()
    if not pids:
        print("Bot is not running.")
        _remove_pid()
        return True

    all_ok = True
    for pid in sorted(pids):
        if not _is_running(pid):
            continue
        print(f"Stopping bot (pid={pid})...")
        if _terminate_tree(pid):
            print("  stopped.")
        else:
            print(f"  ERROR: failed to kill (pid={pid})")
            all_ok = False

    _remove_pid()
    return all_ok


def _start_bot(*, skip_guard: bool = False) -> bool:
    """Start the bot as a detached background process.

    Set *skip_guard* when the caller has already snapshotted old PIDs and
    will kill them after starting (i.e. restart).  In that case the
    duplicate-guard would see the old processes and reject the launch.
    """
    bot_dir = _bot_dir()
    service_file = bot_dir / BOT_SERVICE_NAME
    if not service_file.exists():
        print(f"ERROR: {service_file} not found")
        return False

    # uv project root (pyproject.toml lives in ModexAgent root)
    uv_dir = bot_dir
    while uv_dir != uv_dir.parent:
        if (uv_dir / "pyproject.toml").exists():
            break
        uv_dir = uv_dir.parent
    else:
        uv_dir = bot_dir

    if not skip_guard:
        existing = _find_bot_pids()
        if existing:
            print(f"Bot is already running (pids={existing}). Use 'restart' to replace it.")
            return False

    log_dir = bot_dir / LOG_DIR_NAME
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / LOG_FILE_NAME
    service_rel = service_file.relative_to(uv_dir).as_posix()

    def _try_start(cmd: list[str]) -> subprocess.Popen | None:
        try:
            return subprocess.Popen(cmd, **kwargs)
        except FileNotFoundError:
            return None

    print("Starting bot...")
    log = open(log_path, "a", encoding="utf-8")
    kwargs: dict = {
        "cwd": str(uv_dir),
        "stdout": log,
        "stderr": subprocess.STDOUT,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = _try_start(["uv", "run", "python", service_rel, "--mode", "pool"])
    if proc is None:
        print("'uv' not found, falling back to native python...")
        proc = _try_start([sys.executable, service_rel, "--mode", "pool"])
        if proc is None:
            log.close()
            print("ERROR: neither 'uv' nor 'python' could be found.")
            return False

    _write_pid(proc.pid)
    print(f"Bot started (pid={proc.pid})")
    return True


def restart() -> bool:
    """Start a new bot instance, then stop every *old* bot_service.py process.

    Start-before-stop ensures we survive a self-restart: this script itself
    runs inside the old tree, so we must launch the detached replacement
    *before* tearing down the old tree.
    """
    old_pids = _find_bot_pids()

    if not _start_bot(skip_guard=True):
        return False

    # Give the new child a moment so it appears in the process scan.
    time.sleep(0.5)

    # Compute which PIDs belong to the new instance.
    current_pids = _find_bot_pids()
    new_pids = current_pids - old_pids

    # Kill every old PID that is still alive.
    for pid in sorted(old_pids):
        if pid in new_pids:
            continue  # paranoia — never kill the new instance
        if not _is_running(pid):
            continue
        print(f"Stopping old bot (pid={pid})...")
        # _terminate_root (no /t) so we don't cascade into the new process tree.
        if _terminate_root(pid):
            print("  stopped.")
        else:
            print(f"  WARNING: could not kill old bot (pid={pid})")

    return True


# ── CLI ──────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control the bot process (stop / restart).",
        add_help=False,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["stop", "restart"],
        default="restart",
        help="Command to run (default: restart)",
    )
    parser.add_argument(
        "-h",
        "--help",
        "-help",
        "--h",
        action="help",
        default=argparse.SUPPRESS,
        help="Show this help message and exit.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "stop":
        return 0 if stop() else 1
    return 0 if restart() else 1


if __name__ == "__main__":
    sys.exit(main())
