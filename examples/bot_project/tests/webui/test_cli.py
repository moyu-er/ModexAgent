"""CLI tests for modexbot."""

from __future__ import annotations

from typer.testing import CliRunner

from modexbot.cli import app

runner = CliRunner()


def test_cli_version() -> None:
    """--version emits the version string."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "modexbot" in result.output.lower()


def test_start_help() -> None:
    """start --help shows the port, config, and no-webui options."""
    result = runner.invoke(app, ["start", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output
    assert "--config" in result.output
    assert "--no-webui" in result.output


def test_restart_exists() -> None:
    """restart command exists."""
    result = runner.invoke(app, ["restart", "--help"])
    assert result.exit_code == 0


def test_stop_exists() -> None:
    """stop command exists."""
    result = runner.invoke(app, ["stop", "--help"])
    assert result.exit_code == 0
