"""modexbot CLI — start / stop / restart / status / logs for ModexAgent bot.

Usage::

    modexbot start   [--config DIR] [--port PORT] [--no-webui]
    modexbot stop    [--port PORT]
    modexbot restart [--config DIR] [--port PORT] [--no-webui]
    modexbot status  [--port PORT]
    modexbot logs    [--lines N] [--follow] [--clear]

Run ``modexbot --help`` or ``modexbot <command> --help`` for details.
"""

from __future__ import annotations

import contextlib
import os
import signal as signal_mod
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import typer

from modex_agent._version import __version__
from modexbot.config_model import (
    check_model_config,  # noqa: F401  # kept as a patch target for tests/webui/test_cli.py
)

app = typer.Typer(
    name="modexbot",
    help="ModexAgent bot — multi-channel agent runtime.\n\n"
    "Commands: start, stop, restart, status, install, config, logs. Use <command> --help for details.\n\n"
    "Run 'modexbot install' to rebuild the WebUI after editing frontend sources.",
    no_args_is_help=True,
)

_PKG_ROOT: Path = Path(__file__).resolve().parent.parent
_MODEL_PATH: Path = _PKG_ROOT / "config" / "model.yml"
_REPO_ROOT: Path = _PKG_ROOT.parent.parent


def _resolve_venv_python() -> Path:
    """Find a usable Python for launching bot subprocesses.

    Checks ``bot_project/.venv`` and ``repo_root/.venv`` first (the
    install scripts create the environment at repo root).  Falls back to
    ``sys.executable`` for bundled installs (no venv, deps pre-installed).
    """
    _ = sys.platform
    bins: tuple[str, ...] = ("Scripts",) if _ == "win32" else ("bin",)
    exe_name: str = "python.exe" if _ == "win32" else "python"

    roots = (_REPO_ROOT, _PKG_ROOT)

    for root in roots:
        python = root / ".venv" / bins[0] / exe_name
        if python.is_file():
            return python

    return Path(sys.executable)


_VENV_PYTHON: Path = _resolve_venv_python()
_PID_FILE: Path = _PKG_ROOT / ".modex" / "bot.pid"
_LOG_FILE: Path = _PKG_ROOT / "logs" / "bot.log"


def _resolve_default_port() -> int:
    from bot.config.webui_config import load_webui_port

    return load_webui_port(_PKG_ROOT / "config")


_DEFAULT_PORT: int = _resolve_default_port()
_DEFAULT_LOG_LINES: int = 50


# ── Paths / config ─────────────────────────────────────────────────────────


def _resolve_config(config: Path) -> Path:
    """Resolve config path: if relative, resolve against package root."""
    if not config.is_absolute():
        config = (_PKG_ROOT / config).resolve()
    return config


def _log_file() -> Path:
    """Return the path to the bot log file, ensuring parent dir exists."""
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    return _LOG_FILE


# ── Process discovery ──────────────────────────────────────────────────────
#
# Two-layer discovery used by _stop_running:
#   1. PID file  (fastest, zero subprocess)
#   2. Port scan (netstat / lsof)
#


def _find_processes_by_port(port: int) -> list[int]:
    """Return PIDs of processes listening on *port*."""
    pids: set[int] = set()

    if sys.platform == "win32":
        # netstat -ano | findstr ":<port> "
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    with contextlib.suppress(ValueError, IndexError):
                        pids.add(int(parts[-1]))
        except (subprocess.TimeoutExpired, OSError):
            pass
    else:
        # Linux/macOS: lsof or ss
        for cmd in (
            ["lsof", "-ti", f":{port}"],
            ["ss", "-tlnp"],
        ):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    # lsof -ti returns just PIDs
                    if cmd[0] == "lsof":
                        with contextlib.suppress(ValueError):
                            pids.add(int(line))
                    else:
                        # ss -tlnp: extract pid= from output
                        import re

                        m = re.search(r"pid=(\d+)", line)
                        if m and f":{port}" in line:
                            pids.add(int(m.group(1)))
            except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
                continue
            break  # first successful command

    return sorted(pids)


def _find_processes_by_command(pattern: str) -> list[int]:
    """Return PIDs of processes whose command line contains *pattern*."""
    pids: set[int] = set()

    if sys.platform == "win32":
        # PowerShell: Get-CimInstance Win32_Process | Where CommandLine
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "Get-CimInstance Win32_Process "
                        '| Where-Object {$_.CommandLine -like "*'
                        f"{pattern}"
                        '*"}'
                        " | Select-Object -ExpandProperty ProcessId"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))
        except (subprocess.TimeoutExpired, OSError):
            pass
    else:
        # pgrep -f pattern
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))
        except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
            pass

    return sorted(pids)


_BOT_CMD_MARKERS: tuple[str, ...] = ("modexbot.cli", "modexbot.main", "debug_main")


def _get_command_line(pid: int) -> str | None:
    """Return the command line for *pid*, or None if it cannot be read."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" | Select-Object -ExpandProperty CommandLine',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() or None
        except (subprocess.TimeoutExpired, OSError):
            return None

    # Linux / macOS: prefer /proc, fall back to ps
    try:
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if cmdline_path.exists():
            return cmdline_path.read_text(errors="replace").replace("\x00", " ").strip()
    except (OSError, ValueError):
        pass

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return None


def _is_bot_process(pid: int, *, via_port: bool = False) -> bool:
    """True if *pid* appears to be a modexbot process.

    When *via_port* is True the check is more lenient: any Python process
    listening on the bot port is accepted.  This catches the bot regardless
    of how it was launched (``debug_main.py``, ``python bot_service.py``, etc).
    """
    cmdline = _get_command_line(pid)
    if not cmdline:
        return False
    if any(marker in cmdline for marker in _BOT_CMD_MARKERS):
        return True
    return bool(via_port and "python" in cmdline.lower())


def _is_port_in_use(port: int) -> bool:
    """True if any process is listening on *port*."""
    return len(_find_processes_by_port(port)) > 0


# ── Process kill ───────────────────────────────────────────────────────────


def _kill_process(pid: int) -> bool:
    """Kill a process by PID.  Returns True on success."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/pid", str(pid), "/f"],
                capture_output=True,
                timeout=10,
            )
        else:
            os.kill(pid, signal_mod.SIGTERM)
            time.sleep(0.5)
            if _is_running(pid):
                os.kill(pid, getattr(signal_mod, "SIGKILL", signal_mod.SIGTERM))
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


# ── PID file helpers ───────────────────────────────────────────────────────


def _pid_file() -> Path:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    return _PID_FILE


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
    _PID_FILE.unlink(missing_ok=True)


def _is_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    if sys.platform == "win32":
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


# ── Stop (validated discovery) ─────────────────────────────────────────────


def _stop_running(port: int = _DEFAULT_PORT) -> bool:
    """Stop validated modexbot instance(s).

    Discovery:
      1. PID file — kill only if the process is actually a modexbot.
      2. Port scan — kill only port occupants that are modexbot processes.

    Non-bot processes are never killed.  The current process is always
    excluded so ``restart`` does not accidentally stop its own worker.
    """
    current_pid = os.getpid()
    pids_to_kill: set[int] = set()

    # Layer 1: PID file
    pid = _read_pid()
    if pid is not None and pid != current_pid:
        if _is_running(pid):
            if _is_bot_process(pid):
                pids_to_kill.add(pid)
                typer.echo(f"  Found bot from PID file: pid={pid}")
            else:
                typer.echo(f"  WARNING: PID file points to non-bot process {pid}, ignoring")
                _remove_pid()
        else:
            _remove_pid()  # stale PID file

    # Layer 2: Port scan (always check, may find a different bot)
    port_pids = _find_processes_by_port(port)
    if port_pids:
        for p in port_pids:
            if p == current_pid:
                continue
            if _is_bot_process(p, via_port=True):
                pids_to_kill.add(p)
                typer.echo(f"  Found bot on port {port}: pid={p}")
            else:
                typer.echo(
                    f"  WARNING: port {port} is used by non-bot process {p}, not stopping it"
                )

    if not pids_to_kill:
        return True  # nothing to stop

    for p in sorted(pids_to_kill):
        typer.echo(f"  Stopping pid={p}...")
        if _kill_process(p):
            typer.echo("    stopped.")
        else:
            typer.echo(f"    WARNING: could not stop pid={p}")

    # Wait for processes to die
    for _ in range(10):
        still_alive = [p for p in pids_to_kill if _is_running(p)]
        if not still_alive:
            _remove_pid()
            return True
        time.sleep(0.5)

    still_alive = [p for p in pids_to_kill if _is_running(p)]
    if still_alive:
        typer.echo(f"  WARNING: {len(still_alive)} process(es) still alive: {still_alive}")
        return False

    _remove_pid()
    return True


# ── Bot discovery (read-only, no kill) ────────────────────────────────────


def _discover_bot_pids(port: int) -> dict[int, str]:
    """Discover running modexbot PIDs without stopping them.

    Returns ``{pid: found_via}`` where ``found_via`` is ``"pid_file"``,
    ``"port_scan"``, or ``"both"``.  Excludes the current process.
    """
    current_pid = os.getpid()
    result: dict[int, str] = {}

    # Layer 1: PID file
    pid = _read_pid()
    if pid is not None and pid != current_pid and _is_running(pid) and _is_bot_process(pid):
        result[pid] = "pid_file"

    # Layer 2: Port scan
    port_pids = _find_processes_by_port(port)
    for p in port_pids:
        if p == current_pid:
            continue
        if _is_bot_process(p, via_port=True):
            result[p] = "both" if p in result else "port_scan"

    return result


# ── Process info (cross-platform) ─────────────────────────────────────────


def _parse_ps_etime(etime: str) -> float | None:
    """Parse ``ps -o etime=`` output to seconds.

    Format: ``[[dd-]hh:]mm:ss`` (POSIX).
    """
    etime = etime.strip()
    if not etime:
        return None
    try:
        days = 0
        rest = etime
        if "-" in rest:
            days_str, rest = rest.split("-", 1)
            days = int(days_str)
        parts = rest.split(":")
        if len(parts) == 3:
            return float(days * 86400 + int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
        elif len(parts) == 2:
            return float(days * 86400 + int(parts[0]) * 60 + int(parts[1]))
    except (ValueError, IndexError):
        pass
    return None


def _get_process_uptime_seconds(pid: int) -> float | None:
    """Return process uptime in seconds, or *None* if unavailable (cross-platform)."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}").CreationDate',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            dt_str = result.stdout.strip()
            if dt_str:
                from datetime import datetime as _dt

                dt = _dt.fromisoformat(dt_str)
                return time.time() - dt.timestamp()
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return None

    # Unix: try etimes= (Linux — raw seconds), then etime= (POSIX — formatted)
    for opt in ("etimes=", "etime="):
        try:
            result = subprocess.run(
                ["ps", "-o", opt, "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = result.stdout.strip()
            if not output:
                continue
            if opt == "etimes=":
                for line in output.splitlines():
                    line = line.strip()
                    if line.lstrip("-").isdigit():
                        elapsed = int(line)
                        if elapsed >= 0:
                            return float(elapsed)
            else:
                for line in output.splitlines():
                    elapsed = _parse_ps_etime(line.strip())
                    if elapsed is not None:
                        return elapsed
                break  # etime fallback — don't try anything else
        except (subprocess.TimeoutExpired, OSError, ValueError, FileNotFoundError):
            continue

    return None


def _get_process_memory_mb(pid: int) -> float | None:
    """Return process RSS memory in MB, or *None* if unavailable (cross-platform)."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}").WorkingSetSize',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            val = result.stdout.strip()
            if val and val.isdigit():
                return int(val) / (1024 * 1024)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return None

    # Unix: ps -o rss= (KB)
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        val = result.stdout.strip()
        if val and val.isdigit():
            return int(val) / 1024
    except (subprocess.TimeoutExpired, OSError, ValueError, FileNotFoundError):
        pass

    return None


def _format_uptime(seconds: float) -> str:
    """Format seconds as ``"2h 14m 32s"``."""
    if seconds < 0:
        return "unknown"

    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# ── Run helpers ────────────────────────────────────────────────────────────


def _run_bot(config_str: str, port: int, no_webui: bool) -> None:
    """Run the bot in the current process (blocks until shutdown).

    This is the single shared implementation used by ``start`` and
    ``restart``.  It writes the PID file, creates the service, and runs
    the supervisor loop.
    """
    from functools import partial

    from modexbot.main import create_webui_service, run_with_supervisor

    config = Path(config_str)
    static_dist = None
    if not no_webui:
        dist_path = _PKG_ROOT / "bot" / "web" / "dist"
        static_dist = dist_path if dist_path.exists() else None

    _write_pid(os.getpid())
    try:
        run_with_supervisor(
            partial(create_webui_service, config, port=port, static_dist=static_dist)
        )
    finally:
        _remove_pid()


def _restart_bot(config_str: str, port: int, no_webui: bool) -> None:
    """Stop any running bot, then run a new one in the current process.

    Used by ``restart`` so the restart worker process BECOMES the new
    bot process without spawning yet another child.
    """
    _stop_running(port)
    for i in range(15):
        if not _is_port_in_use(port):
            break
        typer.echo(f"[restart] waiting for port {port} ({i + 1}/15)...")
        time.sleep(1)
    _run_bot(config_str, port, no_webui)


def _launch_subprocess(script: str) -> subprocess.Popen[Any]:
    """Launch *script* via ``python -c`` as a detached background process.

    Prefers the venv Python so the bot runs in the isolated environment.
    Child stdout/stderr are appended to ``logs/bot.log`` so errors are
    visible through ``modexbot logs``.
    """
    python_exe = str(_VENV_PYTHON)
    args = [python_exe, "-c", script]

    # Redirect child stdout/stderr to a SEPARATE file from bot.log so the
    # RotatingFileHandler can rename bot.log during rollover without a
    # competing OS handle (WinError 32 on Windows).
    stdout_log = _log_file().parent / "bot.stdout.log"
    log_stream = stdout_log.open("a", encoding="utf-8", errors="replace")

    kwargs: dict[str, Any] = {
        "cwd": str(_PKG_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": log_stream,
        "stderr": subprocess.STDOUT,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(args, **kwargs)
    log_stream.close()  # child has its own handle; parent doesn't need this
    return proc


def _launch_and_check(script: str, label: str) -> None:
    """Launch a detached subprocess and verify it didn't die immediately."""
    proc = _launch_subprocess(script)
    typer.echo(f"  pid: {proc.pid}")

    time.sleep(2)
    if proc.poll() is not None:
        retcode = proc.returncode
        typer.echo(f"  ERROR: {label} exited immediately with code {retcode}.")
        recent = _tail_file(_log_file(), 10)
        if recent:
            typer.echo("  Recent log output:")
            for line in recent:
                typer.echo(f"    {line}")
        else:
            typer.echo("  Run 'modexbot logs' to see details.")
        raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-v", help="Show version", is_eager=True),
) -> None:
    """ModexAgent bot — multi-channel agent runtime.

    Run ``modexbot <command> --help`` for command-specific usage.
    """
    if version:
        typer.echo(f"modexbot {__version__}")
        raise typer.Exit(0)


@app.command("start")
def start(
    config: Path = typer.Option(  # noqa: B008
        Path("config"),
        "--config",
        "-c",
        help="Path to config directory (default: config/)",
        file_okay=False,
        dir_okay=True,
    ),
    port: int = typer.Option(  # noqa: B008
        _DEFAULT_PORT, "--port", "-p", help="WebUI listen port"
    ),
    no_webui: bool = typer.Option(  # noqa: B008
        False, "--no-webui", help="Backend only, no WebUI frontend"
    ),
) -> None:
    """Start the bot as a detached background process.

    Does not rebuild the WebUI frontend. Run ``modexbot install`` after
    editing frontend source files so the dist is up-to-date.
    """
    config = _resolve_config(config)
    if not config.is_dir():
        typer.echo(f"ERROR: config directory not found: {config}")
        raise typer.Exit(1)

    existing = _read_pid()
    if existing is not None and _is_running(existing):
        typer.echo(
            f"modexbot is already running (pid={existing}). Use 'modexbot restart' to replace it."
        )
        raise typer.Exit(1)

    if _is_port_in_use(port):
        typer.echo(
            f"Port {port} is already in use. Use 'modexbot restart' to stop the old instance first."
        )
        raise typer.Exit(1)

    typer.echo("Starting modexbot...")
    script = f"from modexbot.cli import _run_bot; _run_bot(r'{config}', {port}, {no_webui})"
    _launch_and_check(script, "start worker")
    if not no_webui:
        typer.echo(f"WebUI available at http://localhost:{port}/webui/")
    typer.echo("Done. Use 'modexbot stop' to shut down or 'modexbot logs' to view output.")


@app.command("restart")
def restart(
    config: Path = typer.Option(  # noqa: B008
        Path("config"),
        "--config",
        "-c",
        help="Path to config directory (default: config/)",
        file_okay=False,
        dir_okay=True,
    ),
    port: int = typer.Option(  # noqa: B008
        _DEFAULT_PORT, "--port", "-p", help="WebUI listen port"
    ),
    no_webui: bool = typer.Option(  # noqa: B008
        False, "--no-webui", help="Backend only, no WebUI frontend"
    ),
) -> None:
    """Restart the bot: stop the old instance, then start a fresh one.

    Does not rebuild the WebUI frontend. Run ``modexbot install`` after
    editing frontend source files so the dist is up-to-date.
    """
    config = _resolve_config(config)
    if not config.is_dir():
        typer.echo(f"ERROR: config directory not found: {config}")
        raise typer.Exit(1)

    typer.echo("Restarting modexbot...")
    script = f"from modexbot.cli import _restart_bot; _restart_bot(r'{config}', {port}, {no_webui})"
    _launch_and_check(script, "restart worker")
    if not no_webui:
        typer.echo(f"WebUI available at http://localhost:{port}/webui/")
    typer.echo("Restart in progress. Use 'modexbot logs' to view output.")


@app.command("_run", hidden=True)
def _run(
    config: Path = typer.Option(  # noqa: B008
        Path("config"),
        "--config",
        "-c",
        help="Config directory",
        file_okay=False,
        dir_okay=True,
    ),
    port: int = typer.Option(  # noqa: B008
        _DEFAULT_PORT, "--port", "-p", help="Port"
    ),
    no_webui: bool = typer.Option(  # noqa: B008
        False, "--no-webui", help="No WebUI"
    ),
) -> None:
    """Internal: run the bot in the foreground (used by tests / direct use)."""
    config = _resolve_config(config)
    if not config.is_dir():
        typer.echo(f"ERROR: config directory not found: {config}")
        raise typer.Exit(1)

    _run_bot(str(config), port, no_webui)


@app.command("stop")
def stop(
    port: int = typer.Option(  # noqa: B008
        _DEFAULT_PORT, "--port", "-p", help="Port to check for running instances"
    ),
) -> None:
    """Stop the running modexbot instance(s) by PID file, port, or command."""
    if _stop_running(port):
        typer.echo("modexbot stopped (or was not running).")
    else:
        raise typer.Exit(1)


@app.command("install")
def install(
    force: bool = typer.Option(  # noqa: B008
        False,
        "--force",
        "-f",
        help="Force rebuild even if frontend is up-to-date",
    ),
) -> None:
    """Rebuild the WebUI frontend after editing source files.

    Does not gate on model configuration — the bot is designed to start
    without a model so the user can configure one via the WebUI
    (Settings → Models) or ``modexbot config`` after first start.
    """
    _build_webui(force=force)


@app.command("config")
def config_cmd() -> None:
    """Interactive wizard for the global model configuration (config/model.yml)."""
    from modexbot.interactive_config import run_config_wizard

    run_config_wizard(_MODEL_PATH)


@app.command("model")
def model_cmd() -> None:
    """Interactive wizard for multi-provider model management (config/model.yml)."""
    from modexbot.interactive_config import run_config_wizard

    run_config_wizard(_MODEL_PATH)


@app.command("status")
def status(
    port: int = typer.Option(  # noqa: B008
        _DEFAULT_PORT, "--port", "-p", help="Port to check for running instances"
    ),
) -> None:
    """Show the bot's running status (PID, port, uptime, memory)."""
    instances = _discover_bot_pids(port)

    if not instances:
        typer.echo("Bot Status: STOPPED")
        typer.echo(f"  Port {port}: not in use")
        pid = _read_pid()
        if pid is not None and not _is_running(pid):
            typer.echo(f"  PID file (.modex/bot.pid) is stale (pid={pid})")
        elif pid is not None:
            typer.echo(
                f"  PID file (.modex/bot.pid) points to pid={pid}, "
                "but the process is not a modexbot instance"
            )
        return

    for pid, found_via in instances.items():
        uptime_s = _get_process_uptime_seconds(pid)
        memory_mb = _get_process_memory_mb(pid)

        typer.echo("Bot Status: RUNNING")
        typer.echo(f"  PID:       {pid}")
        typer.echo(f"  Port:      {port}")
        if uptime_s is not None and uptime_s >= 0:
            typer.echo(f"  Uptime:    {_format_uptime(uptime_s)}")
        if memory_mb is not None:
            typer.echo(f"  Memory:    {memory_mb:.1f} MB")
        typer.echo(f"  WebUI:     http://localhost:{port}/webui/")
        typer.echo(f"  Log:       {_log_file()}")
        typer.echo(f"  Config:    {_PKG_ROOT / 'config'}")
        if found_via == "port_scan":
            typer.echo("  Found via: port scan (PID file is missing or stale)")


def _tail_file(path: Path, lines: int) -> list[str]:
    """Return the last *lines* lines of *path*.

    Streams the file line-by-line so large log files do not blow up memory.
    """
    if not path.is_file():
        return []
    from collections import deque

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=lines)]
    except OSError:
        return []


@app.command("logs")
def logs(
    lines: int = typer.Option(  # noqa: B008
        _DEFAULT_LOG_LINES, "--lines", "-n", help="Number of lines to show"
    ),
    follow: bool = typer.Option(  # noqa: B008
        False, "--follow", "-f", help="Follow log output like tail -f"
    ),
    clear: bool = typer.Option(  # noqa: B008
        False, "--clear", help="Truncate the log file before showing"
    ),
) -> None:
    """Show the bot log. Use -f/--follow to tail continuously. Use --clear to truncate."""
    log_path = _log_file()

    if clear:
        instances = _discover_bot_pids(_DEFAULT_PORT)
        if instances:
            typer.echo("Bot is running — clearing the log while the bot is active.")
            typer.echo("New log entries will appear after this point.")
        try:
            log_path.write_text("", encoding="utf-8")
            typer.echo(f"Log cleared: {log_path}\n")
        except OSError as e:
            typer.echo(f"ERROR: could not clear log file: {e}")
            raise typer.Exit(1) from e

    if not log_path.is_file():
        typer.echo(f"No log file found at {log_path}")
        raise typer.Exit(0)

    for line in _tail_file(log_path, lines):
        typer.echo(line)

    if not follow:
        return

    typer.echo("--- following log (Ctrl+C to stop) ---")
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)
        try:
            while True:
                chunk = f.readline()
                if not chunk:
                    time.sleep(0.2)
                    continue
                typer.echo(chunk.rstrip("\n"))
        except KeyboardInterrupt:
            typer.echo("\n--- stopped following ---")


def _build_webui(force: bool = False) -> None:
    """Build the WebUI frontend: ``npm run build`` in ``webui/``.

    Skips the build when *force* is ``False`` and the dist output is
    newer than every source file in ``webui/src/`` and root configs.

    Runs ``npm install`` first if ``node_modules/`` is missing.
    Exits with code 1 on failure so the bot is not launched with stale assets.
    """
    webui_dir = _PKG_ROOT / "webui"
    if not webui_dir.is_dir():
        typer.echo("WARNING: webui/ directory not found, skipping frontend build")
        return

    dist_index = _PKG_ROOT / "bot" / "web" / "dist" / "index.html"

    if not force and dist_index.exists():
        dist_mtime = dist_index.stat().st_mtime
        src_mtime = _newest_source_mtime(webui_dir)
        if src_mtime <= dist_mtime:
            typer.echo("Frontend is up-to-date, skipping build.")
            return

    node_modules = webui_dir / "node_modules"
    if not node_modules.is_dir():
        typer.echo("Installing frontend dependencies (npm install)...")
        _run_npm("install --no-fund --no-audit --progress=false", webui_dir)

    typer.echo("Building frontend (npm run build)...")
    _run_npm("run build", webui_dir)
    typer.echo("Frontend build complete.")


def _run_npm(args: str, cwd: Path) -> None:
    """Run an npm command via the system shell so user-level PATH is used."""
    full_cmd = f"npm {args}"
    try:
        subprocess.run(
            full_cmd,
            shell=True,
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else str(e)
        typer.echo(f"ERROR: '{full_cmd}' failed:\n{stderr}")
        lowered = stderr.lower()
        if any(
            token in lowered
            for token in ("eacces", "permission denied", "eperm", "operation not permitted")
        ):
            typer.echo("\nHINT: npm's global prefix may be owned by root.")
            typer.echo("  Fix it by running the following (no sudo needed):")
            typer.echo("    npm config set prefix ~/.npm-global")
            typer.echo("    mkdir -p ~/.npm-global/bin")
            typer.echo("    export PATH=~/.npm-global/bin:$PATH")
            typer.echo("  Then re-run this command.")
        raise typer.Exit(1) from e
    except subprocess.TimeoutExpired:
        typer.echo(f"ERROR: '{full_cmd}' timed out")
        raise typer.Exit(1) from None


def _newest_source_mtime(webui_dir: Path) -> float:
    """Return the newest mtime among ``webui/src/`` files and root configs."""
    newest = 0.0
    src_dir = webui_dir / "src"
    if src_dir.is_dir():
        for f in src_dir.rglob("*"):
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
    for cfg in (
        "package.json",
        "tsconfig.json",
        "vite.config.ts",
        "tailwind.config.js",
        "postcss.config.js",
        "index.html",
    ):
        cfg_path = webui_dir / cfg
        if cfg_path.is_file():
            newest = max(newest, cfg_path.stat().st_mtime)
    return newest
