"""modexbot CLI — start / stop / restart / logs for ModexAgent bot.

Usage::

    modexbot start   [--config DIR] [--port PORT] [--no-webui]
    modexbot stop    [--port PORT]
    modexbot restart [--config DIR] [--port PORT] [--no-webui]
    modexbot logs    [--lines N] [--follow]

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

from modexbot.config_env import check_env_llm_config

app = typer.Typer(
    name="modexbot",
    help="ModexAgent bot — multi-channel agent runtime.\n\n"
    "Commands: start, stop, restart, install, config, logs. Use <command> --help for details.\n\n"
    "Run 'modexbot install' to rebuild the WebUI after editing frontend sources.",
    no_args_is_help=True,
)

_PKG_ROOT: Path = Path(__file__).resolve().parent.parent
_ENV_PATH: Path = _PKG_ROOT / ".env"
_REPO_ROOT: Path = _PKG_ROOT.parent.parent


def _resolve_venv_python() -> Path:
    """Find a usable venv Python that has modexbot installed.

    Checks both ``bot_project/.venv`` and ``repo_root/.venv``.  The
    install scripts (install.sh / install.bat) create the environment at
    the repo root, so that is tried first.  A stale ``bot_project/.venv``
    with missing dependencies will be skipped.

    On failure the most likely candidate is still returned so error
    messages show a meaningful path.
    """
    _ = sys.platform
    bins: tuple[str, ...] = ("Scripts",) if _ == "win32" else ("bin",)
    exe_name: str = "python.exe" if _ == "win32" else "python"
    cli_name: str = "modexbot.exe" if _ == "win32" else "modexbot"

    roots = (_REPO_ROOT, _PKG_ROOT)  # repo root first (install scripts default)

    for root in roots:
        bin_dir = root / ".venv" / bins[0]
        python = bin_dir / exe_name
        cli = bin_dir / cli_name
        if python.is_file() and cli.is_file():
            return python

    # Neither is usable — return the repo root path as the most likely target.
    return _REPO_ROOT / ".venv" / bins[0] / exe_name


_VENV_PYTHON: Path = _resolve_venv_python()
_PID_FILE: Path = _PKG_ROOT / ".modex" / "bot.pid"
_LOG_FILE: Path = _PKG_ROOT / "logs" / "bot.log"
_DEFAULT_PORT: int = 21800
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
                        'Get-CimInstance Win32_Process '
                        '| Where-Object {$_.CommandLine -like "*'
                        f'{pattern}'
                        '*"}'
                        ' | Select-Object -ExpandProperty ProcessId'
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


_BOT_CMD_MARKERS: tuple[str, ...] = ("modexbot.cli", "modexbot.main")


def _get_command_line(pid: int) -> str | None:
    """Return the command line for *pid*, or None if it cannot be read."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" | Select-Object -ExpandProperty CommandLine",
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


def _is_bot_process(pid: int) -> bool:
    """True if *pid* appears to be a modexbot process based on its command line."""
    cmdline = _get_command_line(pid)
    if not cmdline:
        return False
    return any(marker in cmdline for marker in _BOT_CMD_MARKERS)


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
                typer.echo(
                    f"  WARNING: PID file points to non-bot process {pid}, ignoring"
                )
                _remove_pid()
        else:
            _remove_pid()  # stale PID file

    # Layer 2: Port scan (always check, may find a different bot)
    port_pids = _find_processes_by_port(port)
    if port_pids:
        for p in port_pids:
            if p == current_pid:
                continue
            if _is_bot_process(p):
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

    # _log_file() ensures parent dir exists internally.
    log_stream = _log_file().open("a", encoding="utf-8", errors="replace")

    kwargs: dict[str, Any] = {
        "cwd": str(_PKG_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": log_stream,
        "stderr": subprocess.STDOUT,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
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
        typer.echo(
            f"  ERROR: {label} exited immediately with code {retcode}."
        )
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
    version: bool = typer.Option(
        False, "--version", "-v", help="Show version", is_eager=True
    ),
) -> None:
    """ModexAgent bot — multi-channel agent runtime.

    Run ``modexbot <command> --help`` for command-specific usage.
    """
    if version:
        typer.echo("modexbot 0.1.0")
        raise typer.Exit(0)


@app.command("start")
def start(
    config: Path = typer.Option(  # noqa: B008
        Path("config"),
        "--config", "-c",
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
            f"modexbot is already running (pid={existing}). "
            "Use 'modexbot restart' to replace it."
        )
        raise typer.Exit(1)

    if _is_port_in_use(port):
        typer.echo(
            f"Port {port} is already in use. "
            "Use 'modexbot restart' to stop the old instance first."
        )
        raise typer.Exit(1)

    typer.echo("Starting modexbot...")
    script = (
        "from modexbot.cli import _run_bot; "
        f"_run_bot(r'{config}', {port}, {no_webui})"
    )
    _launch_and_check(script, "start worker")
    if not no_webui:
        typer.echo(f"WebUI available at http://localhost:{port}/webui/")
    typer.echo("Done. Use 'modexbot stop' to shut down or 'modexbot logs' to view output.")


@app.command("restart")
def restart(
    config: Path = typer.Option(  # noqa: B008
        Path("config"),
        "--config", "-c",
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
    script = (
        "from modexbot.cli import _restart_bot; "
        f"_restart_bot(r'{config}', {port}, {no_webui})"
    )
    _launch_and_check(script, "restart worker")
    if not no_webui:
        typer.echo(f"WebUI available at http://localhost:{port}/webui/")
    typer.echo("Restart in progress. Use 'modexbot logs' to view output.")


@app.command("_run", hidden=True)
def _run(
    config: Path = typer.Option(  # noqa: B008
        Path("config"),
        "--config", "-c",
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
        False, "--force", "-f",
        help="Force rebuild even if frontend is up-to-date",
    ),
) -> None:
    """Rebuild the WebUI frontend after editing source files.

    Checks that ``.env`` contains a complete LLM configuration before building.
    """
    complete, missing = check_env_llm_config(_ENV_PATH)
    if not complete:
        typer.echo("WARNING: LLM configuration in .env is incomplete.")
        typer.echo("Required: LLM_MODEL, LLM_API_KEY, LLM_BASE_URL")
        typer.echo(f"Missing: {', '.join(missing)}")

        try:
            response = input("Run 'modexbot config' now? [Y/n]: ").strip().lower()
        except EOFError:
            response = "n"

        if response in ("", "y", "yes"):
            from modexbot.interactive_config import run_config_wizard

            run_config_wizard(_ENV_PATH)
            complete, missing = check_env_llm_config(_ENV_PATH)
            if not complete:
                typer.echo("LLM configuration is still incomplete. Aborting install.")
                raise typer.Exit(1)
        else:
            typer.echo("Run 'modexbot config' first, then retry 'modexbot install'.")
            raise typer.Exit(1)

    _build_webui(force=force)


@app.command("config")
def config_cmd() -> None:
    """Interactive configuration wizard for .env settings."""
    from modexbot.interactive_config import run_config_wizard

    run_config_wizard(_ENV_PATH)


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
) -> None:
    """Show the bot log. Use -f/--follow to tail continuously."""
    log_path = _log_file()
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
        _run_npm("install", webui_dir)

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
            timeout=120,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else str(e)
        typer.echo(f"ERROR: '{full_cmd}' failed:\n{stderr}")
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
        "package.json", "tsconfig.json", "vite.config.ts",
        "tailwind.config.js", "postcss.config.js", "index.html",
    ):
        cfg_path = webui_dir / cfg
        if cfg_path.is_file():
            newest = max(newest, cfg_path.stat().st_mtime)
    return newest
