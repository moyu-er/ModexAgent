"""CLI tests for modexbot — covers help, version, start/restart launch, and logs."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from modexbot.cli import (
    _launch_subprocess,
    _restart_bot,
    _tail_file,
    app,
)
from typer.testing import CliRunner

runner = CliRunner()


def test_no_args_shows_help() -> None:
    """Running modexbot with no arguments shows help (no_args_is_help=True)."""
    result = runner.invoke(app, [])
    # Typer exits with 0 or 2 depending on version when showing help
    assert result.exit_code in (0, 2)
    assert "start" in result.output
    assert "stop" in result.output
    assert "restart" in result.output
    assert "logs" in result.output


def test_cli_version() -> None:
    """--version emits the version string."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "modexbot" in result.output.lower()


def test_venv_python_path_respects_platform() -> None:
    """_VENV_PYTHON points to the correct interpreter inside the venv."""
    from modexbot.cli import _VENV_PYTHON

    venv_str = str(_VENV_PYTHON)
    if sys.platform == "win32":
        assert ".venv\\Scripts\\python.exe" in venv_str
    else:
        assert ".venv/bin/python" in venv_str


def test_resolve_venv_python_candidates() -> None:
    """_resolve_venv_python checks repo_root/.venv first, then bot_project/.venv."""
    from modexbot.cli import _PKG_ROOT, _REPO_ROOT, _resolve_venv_python

    result = _resolve_venv_python()

    # Either candidate could be returned depending on what exists on this machine.
    valid: list[Path]
    if sys.platform == "win32":
        valid = [
            _REPO_ROOT / ".venv" / "Scripts" / "python.exe",
            _PKG_ROOT / ".venv" / "Scripts" / "python.exe",
        ]
    else:
        valid = [
            _REPO_ROOT / ".venv" / "bin" / "python",
            _PKG_ROOT / ".venv" / "bin" / "python",
        ]
    assert result in valid


def test_start_help() -> None:
    """start --help shows options."""
    result = runner.invoke(app, ["start", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output
    assert "--config" in result.output
    assert "--no-webui" in result.output


def test_restart_help() -> None:
    """restart --help shows options."""
    result = runner.invoke(app, ["restart", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output
    assert "--config" in result.output
    assert "--no-webui" in result.output


def test_stop_help() -> None:
    """stop --help shows the port option."""
    result = runner.invoke(app, ["stop", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output


def test_logs_help() -> None:
    """logs --help shows options."""
    result = runner.invoke(app, ["logs", "--help"])
    assert result.exit_code == 0
    assert "--lines" in result.output
    assert "--follow" in result.output


def test_launch_subprocess_uses_python_c() -> None:
    """_launch_subprocess uses python -c to run the given script."""
    with patch("modexbot.cli.subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        _launch_subprocess("print('hello')")
        args = mock_popen.call_args[0][0]
        # The launcher prefers the venv interpreter when available.
        assert Path(args[0]).name.lower().startswith("python")
        assert args[1] == "-c"
        assert args[2] == "print('hello')"


def test_launch_subprocess_redirects_to_log() -> None:
    """_launch_subprocess appends stdout/stderr to logs/bot.log."""
    tmp_path = Path(tempfile.mktemp(suffix=".log"))
    try:
        with patch("modexbot.cli.subprocess.Popen") as mock_popen, \
             patch("modexbot.cli._log_file", return_value=tmp_path):
            mock_popen.return_value = MagicMock()
            _launch_subprocess("pass")
            kwargs = mock_popen.call_args[1]
            assert kwargs["stdout"] is not None
            assert kwargs["stderr"] is subprocess.STDOUT
            assert kwargs["stdin"] is subprocess.DEVNULL
            kwargs["stdout"].close()
    finally:
        tmp_path.unlink(missing_ok=True)


def test_launch_subprocess_has_cwd() -> None:
    """_launch_subprocess runs from the package root."""
    with patch("modexbot.cli.subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        _launch_subprocess("pass")
        kwargs = mock_popen.call_args[1]
        assert "cwd" in kwargs
        assert Path(str(kwargs["cwd"])).is_absolute()


def test_start_already_running_by_pid() -> None:
    """start exits when an existing process is already running."""
    with (
        patch("modexbot.cli._read_pid", return_value=12345),
        patch("modexbot.cli._is_running", return_value=True),
    ):
        result = runner.invoke(app, ["start", "--no-webui"])
        assert result.exit_code == 1
        assert "already running" in result.output


def test_start_already_running_by_port() -> None:
    """start exits when the port is already in use."""
    with (
        patch("modexbot.cli._read_pid", return_value=None),
        patch("modexbot.cli._is_port_in_use", return_value=True),
    ):
        result = runner.invoke(app, ["start", "--no-webui"])
        assert result.exit_code == 1
        assert "already in use" in result.output


def test_start_spawns_bot_process() -> None:
    """start spawns a detached bot process when nothing is running."""
    with (
        patch("modexbot.cli._read_pid", return_value=None),
        patch("modexbot.cli._is_port_in_use", return_value=False),
        patch("modexbot.cli._launch_subprocess") as mock_launch,
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_launch.return_value = mock_proc

        config_dir = Path(__file__).resolve().parent.parent.parent / "config"
        if not config_dir.is_dir():
            config_dir = Path(__file__).resolve().parent.parent.parent

        result = runner.invoke(app, ["start", "--config", str(config_dir), "--no-webui"])

        assert result.exit_code == 0
        mock_launch.assert_called_once()
        script = mock_launch.call_args[0][0]
        assert "_run_bot" in script
        assert "pid: 12345" in result.output


def _default_config_dir() -> Path:
    config_dir = Path(__file__).resolve().parent.parent.parent / "config"
    if not config_dir.is_dir():
        config_dir = Path(__file__).resolve().parent.parent.parent
    return config_dir


def test_start_prints_webui_url() -> None:
    """start prints the frontend URL when WebUI is enabled."""
    with (
        patch("modexbot.cli._read_pid", return_value=None),
        patch("modexbot.cli._is_port_in_use", return_value=False),
        patch("modexbot.cli._launch_subprocess") as mock_launch,
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_launch.return_value = mock_proc

        result = runner.invoke(app, ["start", "--config", str(_default_config_dir())])

        assert result.exit_code == 0
        assert "http://localhost:21800/webui/" in result.output


def test_start_no_webui_url() -> None:
    """start does not print a WebUI URL when --no-webui is set."""
    with (
        patch("modexbot.cli._read_pid", return_value=None),
        patch("modexbot.cli._is_port_in_use", return_value=False),
        patch("modexbot.cli._launch_subprocess") as mock_launch,
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_launch.return_value = mock_proc

        result = runner.invoke(
            app, ["start", "--config", str(_default_config_dir()), "--no-webui"]
        )

        assert result.exit_code == 0
        assert "http://localhost:21800/webui/" not in result.output


def test_restart_prints_webui_url() -> None:
    """restart prints the frontend URL when WebUI is enabled."""
    with (
        patch("modexbot.cli._read_pid", return_value=None),
        patch("modexbot.cli._is_port_in_use", return_value=False),
        patch("modexbot.cli._launch_subprocess") as mock_launch,
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_launch.return_value = mock_proc

        result = runner.invoke(app, ["restart", "--config", str(_default_config_dir())])

        assert result.exit_code == 0
        assert "http://localhost:21800/webui/" in result.output


def test_restart_no_webui_url() -> None:
    """restart does not print a WebUI URL when --no-webui is set."""
    with (
        patch("modexbot.cli._read_pid", return_value=None),
        patch("modexbot.cli._is_port_in_use", return_value=False),
        patch("modexbot.cli._launch_subprocess") as mock_launch,
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_launch.return_value = mock_proc

        result = runner.invoke(
            app, ["restart", "--config", str(_default_config_dir()), "--no-webui"]
        )

        assert result.exit_code == 0
        assert "http://localhost:21800/webui/" not in result.output


def test_start_validates_config_dir() -> None:
    """start with non-existent config dir errors."""
    result = runner.invoke(app, [
        "start", "--config", "/nonexistent/path/config",
    ])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "error" in result.output.lower()


def test_restart_validates_config_dir() -> None:
    """restart with non-existent config dir should error."""
    result = runner.invoke(app, [
        "restart",
        "--config", "/nonexistent/path/config",
    ])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "error" in result.output.lower()


def test_restart_launches_worker_child() -> None:
    """restart spawns a single worker child that calls _restart_bot."""
    with patch("modexbot.cli._launch_subprocess") as mock_launch:
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_launch.return_value = mock_proc

        config_dir = Path(__file__).resolve().parent.parent.parent / "config"
        if not config_dir.is_dir():
            config_dir = Path(__file__).resolve().parent.parent.parent

        result = runner.invoke(app, ["restart", "--config", str(config_dir), "--no-webui"])

        mock_launch.assert_called_once()
        script = mock_launch.call_args[0][0]
        assert "_restart_bot" in script
        assert "pid: 12345" in result.output


def test_restart_bot_calls_stop_then_run() -> None:
    """_restart_bot calls _stop_running then _run_bot."""
    with (
        patch("modexbot.cli._stop_running") as mock_stop,
        patch("modexbot.cli._is_port_in_use", return_value=False),
        patch("modexbot.cli._run_bot") as mock_run,
    ):
        _restart_bot("/fake/config", 21800, True)
        mock_stop.assert_called_once_with(21800)
        mock_run.assert_called_once_with("/fake/config", 21800, True)


def test_logs_no_file() -> None:
    """logs exits gracefully when log file doesn't exist."""
    with patch("modexbot.cli._log_file", return_value=Path("/nonexistent/log")):
        result = runner.invoke(app, ["logs"])
        assert result.exit_code == 0
        assert "No log file" in result.output


def test_logs_shows_last_lines() -> None:
    """logs shows the last N lines of the log file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False, encoding="utf-8"
    ) as tmp:
        for i in range(60):
            tmp.write(f"line {i}\n")
        tmp_path = Path(tmp.name)

    try:
        with patch("modexbot.cli._log_file", return_value=tmp_path):
            result = runner.invoke(app, ["logs", "--lines", "10"])
            assert result.exit_code == 0
            assert "line 50" in result.output
            assert "line 59" in result.output
            assert "line 49" not in result.output
    finally:
        tmp_path.unlink(missing_ok=True)


def test_tail_file_helper() -> None:
    """_tail_file returns the last N lines."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write("a\nb\nc\nd\n")
        tmp_path = Path(tmp.name)

    try:
        lines = _tail_file(tmp_path, 2)
        assert lines == ["c", "d"]
    finally:
        tmp_path.unlink(missing_ok=True)


def test_config_help() -> None:
    """config --help shows the command exists."""
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0


def test_config_runs_wizard() -> None:
    """config command invokes the interactive wizard."""
    with patch("modexbot.interactive_config.run_config_wizard") as mock_wizard:
        result = runner.invoke(app, ["config"])
        assert result.exit_code == 0
        mock_wizard.assert_called_once()


def test_install_with_complete_env_builds_frontend() -> None:
    """install proceeds to build when .env LLM config is complete."""
    with (
        patch("modexbot.cli.check_env_llm_config", return_value=(True, [])),
        patch("modexbot.cli._build_webui") as mock_build,
    ):
        result = runner.invoke(app, ["install"])
        assert result.exit_code == 0
        mock_build.assert_called_once_with(force=False)


def test_install_with_incomplete_env_prompts_and_exits() -> None:
    """install warns and exits when .env LLM config is incomplete."""
    with (
        patch("modexbot.cli.check_env_llm_config", return_value=(False, ["LLM_API_KEY"])),
        patch("modexbot.cli._build_webui") as mock_build,
        patch("builtins.input", return_value="n") as mock_input,
    ):
        result = runner.invoke(app, ["install"])
        assert result.exit_code == 1
        assert "incomplete" in result.output.lower()
        assert "LLM_API_KEY" in result.output
        mock_input.assert_called_once()
        mock_build.assert_not_called()


def test_install_with_incomplete_env_runs_config_then_builds() -> None:
    """install can launch config wizard and then build if env becomes complete."""
    env_states = [(False, ["LLM_API_KEY"]), (True, [])]
    with (
        patch("modexbot.cli.check_env_llm_config", side_effect=env_states) as mock_check,
        patch("modexbot.cli._build_webui") as mock_build,
        patch("modexbot.interactive_config.run_config_wizard") as mock_wizard,
        patch("builtins.input", return_value="y") as mock_input,
    ):
        result = runner.invoke(app, ["install"])
        assert result.exit_code == 0
        assert mock_check.call_count == 2
        mock_wizard.assert_called_once()
        mock_input.assert_called_once()
        mock_build.assert_called_once_with(force=False)


def test_install_with_incomplete_env_still_incomplete_after_config() -> None:
    """install exits if env is still incomplete after running config wizard."""
    with (
        patch("modexbot.cli.check_env_llm_config", return_value=(False, ["LLM_API_KEY"])),
        patch("modexbot.cli._build_webui") as mock_build,
        patch("modexbot.interactive_config.run_config_wizard") as mock_wizard,
        patch("builtins.input", return_value="y"),
    ):
        result = runner.invoke(app, ["install"])
        assert result.exit_code == 1
        assert "still incomplete" in result.output.lower()
        mock_wizard.assert_called_once()
        mock_build.assert_not_called()


def test_install_with_force_passes_force_flag() -> None:
    """install --force passes force=True to _build_webui."""
    with (
        patch("modexbot.cli.check_env_llm_config", return_value=(True, [])),
        patch("modexbot.cli._build_webui") as mock_build,
    ):
        result = runner.invoke(app, ["install", "--force"])
        assert result.exit_code == 0
        mock_build.assert_called_once_with(force=True)
