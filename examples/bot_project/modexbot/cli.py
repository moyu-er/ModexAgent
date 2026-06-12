"""modexbot CLI — ModexAgent bot example command-line interface."""

from __future__ import annotations

import os
import signal as signal_mod
import subprocess
import sys
import time
from pathlib import Path

import typer

app = typer.Typer(name="modexbot", help="ModexAgent bot — multi-channel agent runtime")

_PKG_ROOT: Path = Path(__file__).resolve().parent.parent
_PID_FILE: Path = _PKG_ROOT / ".modex" / "bot.pid"


def _resolve_config(config: Path) -> Path:
    """Resolve config path: if relative, resolve against package root."""
    if not config.is_absolute():
        config = (_PKG_ROOT / config).resolve()
    return config


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
    _pid_file().unlink(missing_ok=True)


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


def _stop_running() -> bool:
    """Stop the running modexbot process if any. Returns True if stopped or none."""
    pid = _read_pid()
    if pid is None:
        return True
    if not _is_running(pid):
        _remove_pid()
        return True

    typer.echo(f"Stopping modexbot (pid={pid})...")
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/pid", str(pid), "/f"], capture_output=True)
        else:
            os.kill(pid, signal_mod.SIGTERM)
            time.sleep(1)
            if _is_running(pid):
                os.kill(pid, getattr(signal_mod, "SIGKILL", signal_mod.SIGTERM))
    except (OSError, ProcessLookupError):
        pass

    for _ in range(10):
        if not _is_running(pid):
            _remove_pid()
            typer.echo("  stopped.")
            return True
        time.sleep(0.5)

    typer.echo(f"  WARNING: could not stop pid={pid}")
    return False


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-v", help="Show version", is_eager=True),
) -> None:
    if version:
        typer.echo("modexbot 0.1.0")
        raise typer.Exit(0)


# ── Start ───────────────────────────────────────────────────────────────────


@app.command("start")
def start(
    config: Path = typer.Option(
        Path("config"),
        "--config", "-c",
        help="Path to config directory",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    port: int = typer.Option(8080, "--port", "-p", help="WebUI listen port"),
    no_webui: bool = typer.Option(False, "--no-webui", help="Backend only, no WebUI frontend"),
) -> None:
    """Start the bot (backend + WebUI). Use --no-webui for backend only."""
    if not no_webui:
        _ensure_webui_deps()

    existing = _read_pid()
    if existing is not None and _is_running(existing):
        typer.echo(f"modexbot is already running (pid={existing}). Use 'restart' to replace it.")
        raise typer.Exit(1)

    config = _resolve_config(config)

    from modexbot.main import run_with_supervisor

    def _factory():
        from modexbot.main import create_webui_service
        static_dist = None
        if not no_webui:
            dist_path = _PKG_ROOT / "bot" / "web" / "dist"
            static_dist = dist_path if dist_path.exists() else None
        return create_webui_service(config, port=port, static_dist=static_dist)

    _write_pid(os.getpid())
    try:
        run_with_supervisor(_factory)
    finally:
        _remove_pid()


# ── Restart ─────────────────────────────────────────────────────────────────


@app.command("restart")
def restart(
    config: Path = typer.Option(
        Path("config"),
        "--config", "-c",
        help="Path to config directory",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    port: int = typer.Option(8080, "--port", "-p", help="WebUI listen port"),
    no_webui: bool = typer.Option(False, "--no-webui", help="Backend only, no WebUI frontend"),
) -> None:
    """Stop running instance, then start a fresh one."""
    _stop_running()
    # Re-read config in case the stop changed something
    _start_detached(config, port, no_webui)


def _start_detached(config: Path, port: int, no_webui: bool) -> None:
    """Launch modexbot as a detached background process."""
    uv_dir = _PKG_ROOT
    while uv_dir != uv_dir.parent:
        if (uv_dir / "pyproject.toml").exists():
            break
        uv_dir = uv_dir.parent

    args = [sys.executable, "-m", "modexbot.cli", "start",
            "--config", str(config), "--port", str(port)]
    if no_webui:
        args.append("--no-webui")

    typer.echo(f"Starting modexbot (python -m modexbot.cli start)...")

    kwargs: dict = {"cwd": str(uv_dir)}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(args, **kwargs)
    time.sleep(1)
    typer.echo("Done. Check logs/ for output.")


# ── Stop ────────────────────────────────────────────────────────────────────


@app.command("stop")
def stop() -> None:
    """Stop the running modexbot instance."""
    if _stop_running():
        typer.echo("modexbot is not running.")
    else:
        raise typer.Exit(1)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _ensure_webui_deps() -> None:
    try:
        import aiohttp  # noqa: F401
        import websockets  # noqa: F401
    except ImportError as exc:
        typer.echo(
            f"Missing webui dependency: {exc}.\n"
            "Run: pip install 'modex-bot-project[webui]'"
        )
        raise typer.Exit(1) from exc
